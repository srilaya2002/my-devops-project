# MSSQL DR Sync Rebuild — Implementation Guide

> The reverse of [`mssql-dr-failover-implementation.md`](mssql-dr-failover-implementation.md):
> bring a replica that's fallen out of sync — or fallen out of the AG
> entirely — back to a healthy `SYNCHRONIZED` secondary. Grounded in the same
> live AG this whole series builds on, not the generic
> [`mssql-dr-sync-rebuild-design.md`](mssql-dr-sync-rebuild-design.md)
> template (which assumes a listener and an app-facing cutover this lab
> doesn't have). Read the failover guide first — this one reuses its
> `ag-status` endpoint for precheck/postcheck rather than redefining it.

## Design thinking

**Two genuinely different broken states, one endpoint.** "Out of sync" isn't
one problem — in this lab it's two, and they need different fixes:

1. **Still a member of the AG, but data movement is suspended** — a
   connectivity blip, a restart, anything that pauses HADR without removing
   the replica from the group. Fix: `ALTER DATABASE AdventureWorks SET HADR
   RESUME`. Cheap, fast, no data movement needed beyond catching up the log.
2. **Fell out of the AG entirely** — most commonly the old primary after a
   **forced** failover (`FORCE_FAILOVER_ALLOW_DATA_LOSS` leaves the old
   primary's database orphaned from the group, since the new primary may
   have committed transactions the old one never got). Fix: drop the stale
   local copy, `ALTER AVAILABILITY GROUP ... JOIN`, and let
   `SEEDING_MODE=AUTOMATIC` (already set in `alwayson.yml`) re-copy the
   database over the network — the same mechanism that seeded vm2 in the
   original build.

The task file below checks which state a replica is in and does the right
one — same "detect, then act" shape as `alwayson.yml`'s own idempotency
checks, so it's safe to call on a replica that's actually already fine (both
branches no-op, it just falls through to the final sync-state wait).

**Why it reuses `ag-status` rather than defining its own precheck.** The
question "is this replica healthy?" doesn't change between the failover and
sync-rebuild guides — it's the same DMV query either way. Duplicating it
into a second task file would just be two copies to keep in sync (pun
intended) for no benefit.

**Why automatic seeding, not a manual backup/restore.** The generic design
doc suggests "copy the latest backup from the primary" — that's exactly what
`backup.yml`/`restore.yml` already automate for the *initial* build. For
*rejoining* an existing AG, though, `SEEDING_MODE=AUTOMATIC` (set when the AG
was created) means SQL Server does this itself once you rejoin — no
controller-relay dance needed. Doing it manually here would just be
reimplementing what the AG's own seeding mode already does.

## Prerequisites

- The AG from `mssql-fastapi-build-from-scratch.md`, built and (at some
  point) healthy.
- A replica that actually needs rebuilding — the easiest way to get one on
  purpose, for practicing this: follow the failover guide's forced-failover
  drill (`systemctl stop mssql-server` on vm1, then
  `POST /deploy/failover?target=vm2&mode=forced`). vm1 is now the broken
  replica this guide fixes.

## Stage-by-stage code

### 1. New role task: `ansible/roles/mssql/tasks/sync_rebuild.yml`

Invoked with `--limit` against the single replica being rebuilt, same
pattern as `failover.yml`.

```yaml
---
# Resynchronize or rejoin a replica that has fallen behind or dropped out of
# the Always On AG. Safe to re-run. Invoked with --limit against the single
# replica being rebuilt. Target should be a SECONDARY, not the current primary.

- name: Verify MSSQL connectivity on the rebuild target
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "SELECT 1"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - sync_rebuild

- name: Check whether this replica is currently a member of the AG
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM sys.availability_replicas ar WHERE ar.replica_server_name = '{{ vmware_name }}' AND EXISTS (SELECT 1 FROM sys.dm_hadr_availability_replica_states ars WHERE ars.replica_id = ar.replica_id)"
  register: ag_membership_check
  changed_when: false
  tags:
    - sync_rebuild

# --- Path A: still a member, just suspended ---

- name: Check whether AdventureWorks data movement is suspended on this replica
  # Filtered to this replica specifically (join sys.availability_replicas and
  # match vmware_name) -- without it, once both replicas in the AG are
  # healthy this DMV returns one row per replica sharing the database, not
  # one row total, and stdout becomes e.g. "0\n0" instead of "0". See
  # "Live-testing findings" below (2026-08-31) for what that breaks.
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT ISNULL(drs.is_suspended, 0) FROM sys.databases d LEFT JOIN sys.dm_hadr_database_replica_states drs ON drs.database_id = d.database_id LEFT JOIN sys.availability_replicas ar ON ar.replica_id = drs.replica_id WHERE d.name = 'AdventureWorks' AND (ar.replica_server_name = '{{ vmware_name }}' OR ar.replica_server_name IS NULL)"
  register: suspended_check
  changed_when: false
  when: (ag_membership_check.stdout | trim) == '1'
  tags:
    - sync_rebuild

- name: Resume data movement (still a member, just suspended)
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER DATABASE AdventureWorks SET HADR RESUME"
  when: (ag_membership_check.stdout | trim) == '1' and (suspended_check.stdout | trim) == '1'
  changed_when: true
  tags:
    - sync_rebuild

# --- Path B: fell out of the AG entirely -- rejoin, let automatic seeding reseed it ---

- name: Drop a stale standalone copy of AdventureWorks before rejoining
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "
    IF EXISTS (
      SELECT * FROM sys.databases d
      WHERE d.name = 'AdventureWorks'
      AND NOT EXISTS (SELECT 1 FROM sys.dm_hadr_database_replica_states drs WHERE drs.database_id = d.database_id)
    )
    BEGIN
      ALTER DATABASE AdventureWorks SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
      DROP DATABASE AdventureWorks;
    END
    "
  when: (ag_membership_check.stdout | trim) == '0'
  changed_when: true
  tags:
    - sync_rebuild

- name: Rejoin the Availability Group
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER AVAILABILITY GROUP [{{ ag_name }}] JOIN WITH (CLUSTER_TYPE = {{ ag_cluster_type }})"
  when: (ag_membership_check.stdout | trim) == '0'
  changed_when: true
  tags:
    - sync_rebuild

- name: Grant automatic seeding permission (needed again after rejoin)
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER AVAILABILITY GROUP [{{ ag_name }}] GRANT CREATE ANY DATABASE"
  when: (ag_membership_check.stdout | trim) == '0'
  changed_when: true
  tags:
    - sync_rebuild

# --- Verify, either path ---

- name: Wait for AdventureWorks to reach SYNCHRONIZED state on this replica
  # Same fix as the suspended-check above -- filtered to this replica via
  # sys.availability_replicas, so stdout is a single value the `until`
  # string-equality check can actually match. Unfiltered, this always
  # times out (retries: 30) the moment the OTHER replica is also healthy,
  # even though this replica itself reached SYNCHRONIZED immediately.
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT drs.synchronization_state_desc FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id JOIN sys.availability_replicas ar ON ar.replica_id = drs.replica_id WHERE d.name = 'AdventureWorks' AND ar.replica_server_name = '{{ vmware_name }}'"
  register: sync_wait
  retries: 30
  delay: 10
  until: (sync_wait.stdout | trim) == 'SYNCHRONIZED'
  changed_when: false
  tags:
    - sync_rebuild

- name: Display final sync state
  debug:
    msg: "This replica's AdventureWorks is now {{ sync_wait.stdout | trim }}"
  tags:
    - sync_rebuild
```

Path B's reseed goes over the network from whichever replica is currently
`PRIMARY` — for a large database this is the slow path (`retries: 30, delay:
10` = up to 5 minutes of polling; raise it for a bigger database than the
lab's AdventureWorks sample).

### 2. New playbook: `ansible/playbooks/sync_rebuild.yml`

```yaml
---
- name: Resynchronize or rejoin a replica to the Always On AG
  hosts: mssql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run sync-rebuild tasks
      include_role:
        name: mssql
        tasks_from: sync_rebuild.yml
      tags:
        - sync_rebuild
```

### 3. `app/deployer.py` addition

```python
def deploy_sync_rebuild(self, task_id: str, target: str) -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "sync_rebuild.yml",
            limit=target,
            extra_vars=self._build_extra_vars(),
        ),
    )
```

### 4. `app/routes/deploy.py` addition

```python
@router.post("/sync-rebuild")
async def deploy_sync_rebuild(background_tasks: BackgroundTasks, target: str):
    """Resynchronize or rejoin `target` ('vm1' or 'vm2') to the Always On AG.

    Resumes suspended data movement if the replica is still a member, or
    rejoins (and lets automatic seeding reseed it) if it fell out of the AG.
    """
    if target not in ("vm1", "vm2"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm1' or 'vm2'")

    logger.info(f"Received deployment request - Sync rebuild {target}")
    try:
        task_id = deployer.start_task(f"sync-rebuild-{target}")
        background_tasks.add_task(deployer.deploy_sync_rebuild, task_id, target)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": f"Sync rebuild for {target} started",
            "engine": "ansible",
            "playbook": "sync_rebuild.yml",
            "target": target,
            "estimated_duration_minutes": 15,
        }
    except Exception as e:
        logger.error(f"Error initiating sync rebuild: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate sync rebuild: {str(e)}")
```

(`ag-status` — used below for precheck/postcheck — is the one added in the
failover guide; nothing new to add for it here.)

## Run it — CLI

**All-in-one** (the task file already figures out which of the two broken
states applies and does the right thing):
```bash
curl -X POST "http://localhost:8000/api/v1/deploy/sync-rebuild?target=vm1"
tail -f logs/ansible.log
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0]'
```

**Stage by stage** — precheck, rebuild, postcheck, same three-phase shape as
the failover guide:
```bash
# 1. Precheck: confirm vm1 really is unhealthy/out of the AG
curl -X POST http://localhost:8000/api/v1/deploy/ag-status
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0].results'

# 2. Rebuild
curl -X POST "http://localhost:8000/api/v1/deploy/sync-rebuild?target=vm1"
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0]'

# 3. Postcheck: confirm vm1 is SECONDARY / SYNCHRONIZED again
curl -X POST http://localhost:8000/api/v1/deploy/ag-status
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0].results'
```

**Full DR round-trip drill** (continuing the failover guide's drill, so you
practice the whole loop end to end):
```bash
# vm1 was stopped, vm2 was force-failed-over to primary (failover guide)
sudo systemctl start mssql-server   # on vm1

curl -X POST "http://localhost:8000/api/v1/deploy/sync-rebuild?target=vm1"
# vm1 is now SECONDARY again, synchronized to vm2

# optional: fail back to vm1 as primary once it's healthy
curl -X POST "http://localhost:8000/api/v1/deploy/failover?target=vm1&mode=planned"
```

## Run it — Swagger UI

**All-in-one:**
1. Expand **POST /api/v1/deploy/sync-rebuild**, **Try it out**, fill
   `target=vm1`, **Execute**.
2. **GET /api/v1/deploy/history** → **Execute**, watch `status` go
   `running` → `success`/`failed`.

**Stage by stage:**
1. **POST /api/v1/deploy/ag-status** → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** to see vm1's broken state.
2. **POST /api/v1/deploy/sync-rebuild** with `target=vm1` → **Execute**.
3. **POST /api/v1/deploy/ag-status** → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** to confirm vm1 is
   `SECONDARY`/`SYNCHRONIZED`.

## Verification

```bash
/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P '<sa_password>' -h -1 -W -Q \
  "SELECT ar.replica_server_name, ars.role_desc, ars.synchronization_health_desc FROM sys.dm_hadr_availability_replica_states ars JOIN sys.availability_replicas ar ON ar.replica_id = ars.replica_id"

/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P '<sa_password>' -h -1 -W -Q \
  "SELECT d.name, drs.synchronization_state_desc, drs.is_suspended FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id"
```
Expect the rebuilt replica's row to show `SECONDARY` / `HEALTHY` and
`AdventureWorks` / `SYNCHRONIZED` / `is_suspended = 0`.

## Live-testing findings

**2026-08-31 — ran `sync-rebuild?target=vm1` against an already-healthy
`PRIMARY` (vm1); it made zero changes but still ended `failed` after 5
minutes, because of the multi-row query bug fixed above.**

Ran it via `curl` against vm1 while vm1 was `PRIMARY`/`HEALTHY` and vm2 was
`SECONDARY`/`HEALTHY` (both fine — this was a "does it no-op safely"
check, not a real rebuild). Result:
- `ag_membership_check` → `1` (correct — still a member)
- `suspended_check` → **`"0\n0"`** — two rows, both `0`
- Both mutating branches (Resume / drop+rejoin+grant) correctly skipped —
  `changed: false`/`skipped` for every task, so the run made zero changes
- Final "Wait for AdventureWorks to reach SYNCHRONIZED" polled for the
  full 5 minutes (30 × 10s) and then failed with stdout
  **`"SYNCHRONIZED\nSYNCHRONIZED"`** — both replicas genuinely
  synchronized, reported correctly, and the task still failed

**Root cause:** neither the `suspended_check` query nor the final
`sync_wait` query filters `sys.dm_hadr_database_replica_states` down to a
single replica — `failover.yml`'s equivalent queries already do this via a
`JOIN sys.availability_replicas ar ... WHERE ar.replica_server_name =
'{{ vmware_name }}'`, but these two queries in `sync_rebuild.yml` never got
that filter. Once *both* replicas in the AG are healthy, this DMV returns
one row per replica sharing the database — so the query returns 2 rows
instead of 1, and the `until: (x.stdout | trim) == '<single value>'`
checks can never match a two-line string, no matter how healthy the AG
actually is. **Fix applied both here and to the actual
`ansible/roles/mssql/tasks/sync_rebuild.yml` on disk** — confirmed working
in the two drills below, both of which completed in seconds instead of
timing out after 5 minutes.

**Practical impact:** harmless when the target is already healthy (as
here) — the run just wastes 5 minutes and reports a false `failed`. Not
harmless if you're relying on the task's *return code* to drive automation
(e.g. gating a follow-up step on `sync-rebuild` succeeding) — right now a
perfectly healthy rebuild reports failure indistinguishable, from the
API's perspective, from a real one.

---

**2026-08-31 — Path A drill: clean success in ~6 seconds, first try.**

With vm1 `PRIMARY`/`HEALTHY` and vm2 `SECONDARY`/`HEALTHY`, ran
`ALTER DATABASE AdventureWorks SET HADR SUSPEND` directly on vm2, confirmed
`is_suspended = 1` / `NOT SYNCHRONIZING`, then called
`sync-rebuild?target=vm2`. Result: `ag_membership_check` → `1`,
`suspended_check` → `1` (single row, thanks to the fix above), "Resume data
movement" ran (`changed: true`), Path B's three tasks all correctly
skipped, final wait matched `SYNCHRONIZED` on the very first attempt.
`ok=7, changed=1, failed=0`, done in under 6 seconds end to end. This is
the boring, common case working exactly as designed — worth having
actually seen it happen at least once, since every other run in this
series so far has taken Path B or hit the query bug instead.

---

**2026-08-31 — Path B drill: also succeeded cleanly, in ~27 seconds, no
manual recovery needed. This disproves the "open question" originally
written here — see below for what that question actually was and why the
answer turned out to be simpler than expected.**

With the AG healthy again after the Path A drill, ran on vm2:
```sql
ALTER AVAILABILITY GROUP [AG1] OFFLINE;
DROP AVAILABILITY GROUP [AG1];
```
confirmed `SELECT COUNT(*) FROM sys.availability_groups` → `0` on vm2, then
called `sync-rebuild?target=vm2` **5 seconds later** — deliberately fast,
to catch it before any primary-side re-propagation could plausibly kick
in. Result: `ag_membership_check` → `0` (correctly detected as fallen out
of the AG), "Drop a stale standalone copy" ran and hit a harmless SQL error
(`Msg 5052: ALTER DATABASE is not permitted while a database is in the
Restoring state` — AdventureWorks was mid-transition, not a plain
standalone copy, so this no-op'd safely rather than actually dropping
anything), **"Rejoin the Availability Group" (`JOIN WITH (CLUSTER_TYPE =
NONE)`) succeeded outright — no 41106, no stale in-memory cache, no
restart needed**, "Grant automatic seeding permission" succeeded (correctly
scoped to vm2 via `--limit`, per the failover guide's "grant on the
receiving replica, not the primary" finding), and the final wait matched
`SYNCHRONIZED` on the *second* attempt (~13 seconds after `JOIN`) — seeding
a database this small is nearly instant. `ok=8, changed=3, failed=0`.
Confirmed both replicas `SYNCHRONIZED`/`HEALTHY` afterward with no further
action.

**Why this contradicts the "open question" this section originally posed**
(the theory, based on the 2026-08-30 split-brain recovery, that a plain
secondary-side `DROP` wouldn't stick because the primary would silently
re-declare the replica): **it doesn't contradict that finding — it shows
that finding was specific to recovering from an actual split-brain, not a
general property of `OFFLINE`+`DROP`+`JOIN`.** The 2026-08-30 incident that
needed `REMOVE REPLICA`/`ADD REPLICA` had a lot more going on before that
point: two concurrent `failover.yml` runs racing each other, a genuine
split-brain (both replicas independently `PRIMARY`), several failed `JOIN`
attempts, and a service restart in between — plenty of opportunity for
stale internal HADR state to accumulate beyond what a simple `DROP`
clears. This drill started from a single, healthy, non-split AG and broke
exactly one thing (one replica's local membership) — and the `JOIN` that
had failed with 41106 three separate times during the split-brain recovery
worked immediately here with no extra steps. **Conclusion: `sync_rebuild.yml`'s
Path B, as originally written — no `ADD REPLICA` needed — is correct for
the case it's actually meant to handle** (a replica that fell out of an
otherwise-healthy AG). The `REMOVE REPLICA`/`ADD REPLICA` dance from the
failover guide is a **split-brain-specific** recovery step, not something
Path B needs to do routinely — the speculative fix proposed earlier in this
section is **not needed** and hasn't been applied.

**One real gap this drill did surface:** "Drop a stale standalone copy of
AdventureWorks" reports `changed: true` even when its inner `sqlcmd` call
errors out (`Msg 5052`) rather than actually dropping anything — the task's
`changed_when: true` is unconditional, not keyed off whether the `IF
EXISTS` block's body actually ran. Harmless here (the error was itself a
no-op-equivalent outcome — nothing needed dropping), but worth knowing:
this task's "changed" status can't be trusted to mean "a database was
actually dropped."

## Simulating failures to test this guide

The AG has to actually be broken for `sync-rebuild` to do anything
observable — running it against an already-healthy replica only exercises
the no-op path (see the first Live-testing finding above). Two deliberate
ways to break it, one per code path — both confirmed working end to end on
2026-08-31, timings below are real, not estimates.

**Path A drill — suspend data movement (safe, ~6 seconds observed):**
```bash
# on the current SECONDARY (check with ag-status first if unsure which one)
/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P '<sa_password>' -Q "ALTER DATABASE AdventureWorks SET HADR SUSPEND"
```
Confirm via `ag-status`: the suspended replica should show
`is_suspended = 1`, `NOT SYNCHRONIZING`. Then:
```bash
curl -X POST "http://localhost:8000/api/v1/deploy/sync-rebuild?target=<that vm>"
```
Expect: `suspended_check` reads `1`, "Resume data movement" runs
(`changed: true`), final wait reaches `SYNCHRONIZED` on the first attempt.
Confirmed exactly this on the first try — see the Live-testing finding
above for the full task-by-task trace.

**Path B drill — force a replica fully out of the AG (safe against a
SECONDARY; confirmed ~27 seconds end to end, no manual recovery needed —
**never do this against the current PRIMARY**):**
```bash
# on the current SECONDARY
/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P '<sa_password>' -Q "ALTER AVAILABILITY GROUP [AG1] OFFLINE"
/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P '<sa_password>' -Q "DROP AVAILABILITY GROUP [AG1]"
```
Confirm `SELECT COUNT(*) FROM sys.availability_groups` reads `0` locally,
then call `sync-rebuild` on that target right away:
```bash
curl -X POST "http://localhost:8000/api/v1/deploy/sync-rebuild?target=<that vm>"
```
Expect: `ag_membership_check` reads `0`, "Drop a stale standalone copy"
runs (may harmlessly error with `Msg 5052` if the database is still mid
`RESTORING` — that's fine, nothing needed dropping), `JOIN` succeeds
outright with no 41106, `GRANT` succeeds, and the final wait reaches
`SYNCHRONIZED` within a couple of poll attempts for a database this small.
No `REMOVE REPLICA`/`ADD REPLICA` needed — that dance is specific to
recovering from an actual split-brain (see the Live-testing finding above
for why), not something this drill requires.

## What could still go wrong (learning notes)

- **Path B can take a while on a bigger database.** Automatic seeding
  copies the whole database over the network; the 5-minute poll window in
  the task file is sized for AdventureWorks (a few hundred MB), not a
  production-sized database.
- **If Path B's rejoin itself fails**, check `logs/ansible.log` for the
  actual `sqlcmd` error — the most likely cause in this lab is the same
  ownership issue from the original build (bug #1/#5 in the build-history
  doc): if `data_dir` ever gets re-created by hand instead of through
  `configure.yml`, it can silently end up `root:root` again.
- **This doesn't handle a split-brain** (both replicas independently
  believe they're primary after a forced failover, if the "old" primary
  comes back before you've run sync-rebuild on it). Always run
  `ag-status` first and read it before assuming which side is stale —
  `sync_rebuild.yml`'s Path B drops whatever local copy isn't part of a live
  AG membership, which is only safe to do to the side you've confirmed is
  the stale one.
