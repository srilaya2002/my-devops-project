# Oracle Data Guard Sync Rebuild — Implementation Guide

> The reverse of
> [`oracle-dataguard-failover-implementation.md`](oracle-dataguard-failover-implementation.md):
> bring a standby that's fallen behind — or fallen out of the Data Guard
> configuration entirely — back to a healthy, synchronized member.
> Grounded in the same live pair this whole series builds on, not the
> generic
> [`oracle-dataguard-sync-rebuild-design.md`](oracle-dataguard-sync-rebuild-design.md)
> template (which assumes an app-facing cutover this lab doesn't have).
> Read the failover guide first — this one reuses its `POST /deploy/validate`
> endpoint for precheck/postcheck rather than redefining it, the same
> relationship
> [`mssql-dr-sync-rebuild-implementation.md`](mssql-dr-sync-rebuild-implementation.md)
> has to its own failover guide.
>
> Same caveat as the rest of this series: **a plan to review, not a record
> of a working build** — nothing here has run against `devops_VM3`/
> `devops_VM4` yet.

## Design thinking

**Two genuinely different broken states, one endpoint — same shape as the
MSSQL guide, different detection mechanism.** "Out of sync" is two problems
here too:

1. **Still a broker member, but redo apply has stalled** — a network blip,
   a restart, anything that stops the managed-recovery process (`MRP0`)
   without removing the database from the configuration. Fix: cancel and
   restart managed recovery.
2. **Fell out of the configuration entirely** — the standard case is the
   **old primary after a forced failover**: `FAILOVER TO` promotes the
   surviving standby without coordinating with the old primary at all (it
   can't — that's the whole premise of "forced"), so the broker marks the
   old primary's entry `ERROR`. Fix: `REINSTATE DATABASE`, which uses
   Flashback Database to rewind the old primary to a point before it
   diverged and turns it into a standby of the new primary — no network
   copy needed, unlike a full rebuild.

Unlike MSSQL, where AG membership is a binary in/out of
`sys.availability_replicas`, the broker already tracks a richer per-database
status (`SUCCESS`/`WARNING`/`ERROR`) via `dgmgrl SHOW DATABASE <name>` — the
task file below branches on that directly instead of reinventing a
membership count.

**The gotcha that makes this guide worth reading before you write your own
version: don't trust `v$database.database_role` to decide which path
applies.** After a forced failover, the *old* primary's own instance never
got the memo — query `v\$database` locally on it and it will often still
proudly report `database_role = PRIMARY`, right up until `REINSTATE`
actually runs. That field reflects what the local instance believes about
itself, not what the broker configuration (which has moved on) says is
true. This task file branches on the broker's `SHOW DATABASE` status
instead, specifically because it's the one source that isn't fooled by an
orphaned instance's stale self-image — the same lesson the failover guide's
`db_unique_name`-vs-live-role note was pointing at, one layer further in.

**Why it reuses `POST /deploy/validate` rather than defining its own
precheck.** Same reasoning as the MSSQL guide: "is this database healthy?"
doesn't change between the failover and sync-rebuild guides. `validate.yml`
already reports role, open mode, lag, and full broker status — precheck and
postcheck below are just two more calls to it.

**Why `REINSTATE`, not a manual backup/restore or a repeat of the original
duplication.** The generic design doc suggests "copy the latest backup from
the primary" — that's exactly what `standby.yml`'s RMAN active-database
duplication already automates for the *initial* build. Re-running that
against an old primary needing rebuild would work, but `REINSTATE DATABASE`
is cheaper when it's available: it uses flashback logs already sitting in
the old primary's own fast recovery area, so — if the flashback window
covers however long it's been since the failover — no data moves over the
network at all. Doing a full re-duplication first would be reaching for the
expensive tool before trying the cheap one Oracle built specifically for
this scenario.

## Prerequisites

- The Data Guard pair from `oracle-fastapi-build-from-scratch.md`, built
  and (at some point) healthy — confirm with
  `POST /deploy/validate` before assuming anything below will work.
- **Flashback Database must be enabled on both vm3 and vm4 — the build
  guide doesn't do this yet.** `REINSTATE DATABASE` depends on it entirely;
  without it, Path B below has no fast option and falls straight to "not
  automated yet" (see the last section). Enable it by hand on both hosts
  before practicing this guide, as the oracle user:
  ```bash
  sqlplus / as sysdba <<'EOF'
  ALTER DATABASE FLASHBACK ON;
  SELECT flashback_on FROM v$database;
  EOF
  ```
  `db_fra_size_mb: 20480` (from the build guide's defaults) gives the FRA
  room for flashback logs without a config change; `db_flashback_retention_target`
  defaults to 1440 minutes (24 hours) — plenty for lab use, but it's the
  ceiling on how long you can wait after a failover before `REINSTATE`
  stops being an option. This really belongs in `primary_db.yml`/
  `standby_prep.yml` in a future revision of the build guide rather than a
  manual step here — noted, not fixed, in this pass.
- A standby that actually needs rebuilding — the easiest way to get one on
  purpose, for practicing this: follow the failover guide's forced-failover
  drill (shut down vm3, then `POST /deploy/failover?target=vm4`). vm3 is
  now the broken database this guide fixes.

## Stage-by-stage code

### 1. New role task: `ansible/roles/oracle_dg/tasks/sync_rebuild.yml`

Invoked with `--limit` against the single database being rebuilt, same
pattern as `switchover.yml`/`failover.yml`. Target should be the broken
standby, not the current primary — but see the note above about why this
file can't safely check that by querying `v$database` on the target itself.

```yaml
---
# Resynchronize or reinstate a database that has fallen behind or dropped
# out of the Data Guard configuration. Safe to re-run. Invoked with --limit
# against the single database being rebuilt.

- name: Verify Oracle connectivity on the rebuild target
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SELECT 1 FROM dual;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - sync_rebuild

- name: Compute this host's db_unique_name
  set_fact:
    my_db_unique_name: "{{ (oracle_role == 'primary') | ternary(primary_db_unique_name, standby_db_unique_name) }}"
  tags:
    - sync_rebuild

- name: Check this database's status as the broker sees it
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ my_db_unique_name | upper }} "SHOW DATABASE {{ my_db_unique_name }}"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: broker_status_check
  changed_when: false
  failed_when: false
  tags:
    - sync_rebuild

# --- Path A: still a healthy broker member, apply just stalled ---

- name: Restart managed recovery (still a member, apply stalled)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;
    ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: "'Database Status:' in broker_status_check.stdout and 'ERROR' not in broker_status_check.stdout"
  changed_when: true
  tags:
    - sync_rebuild

# --- Path B: fell out of the configuration -- reinstate via flashback ---

- name: Attempt REINSTATE DATABASE (uses Flashback Database, no network copy)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ my_db_unique_name | upper }} <<EOF
    REINSTATE DATABASE {{ my_db_unique_name }};
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: reinstate_result
  when: "'ERROR' in broker_status_check.stdout"
  changed_when: true
  failed_when: false
  tags:
    - sync_rebuild

- name: Fail clearly if REINSTATE did not succeed
  fail:
    msg: >-
      REINSTATE DATABASE {{ my_db_unique_name }} did not complete cleanly
      (see reinstate_result.stdout above -- likely the Flashback Database
      window doesn't cover how long ago the failover happened). This
      guide's automation stops here; a full re-duplication of a
      role-reversed host isn't implemented yet -- see "What could still go
      wrong" in oracle-dataguard-sync-rebuild-implementation.md for the
      manual fallback.
  when: "'ERROR' in broker_status_check.stdout and reinstate_result is defined and 'ORA-' in reinstate_result.stdout"
  tags:
    - sync_rebuild

# --- Verify, either path ---

- name: Wait for apply lag to reach zero
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT value FROM v\$dataguard_stats WHERE name = 'apply lag';
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: lag_wait
  retries: 30
  delay: 10
  until: (lag_wait.stdout | trim) == '+00 00:00:00' or (lag_wait.stdout | trim) == ''
  changed_when: false
  tags:
    - sync_rebuild

- name: Display final broker status
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ my_db_unique_name | upper }} "SHOW DATABASE {{ my_db_unique_name }}"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: final_status
  changed_when: false
  tags:
    - sync_rebuild

- name: Display result
  debug:
    msg: "{{ final_status.stdout_lines }}"
  tags:
    - sync_rebuild
```

Two things worth being upfront about here, in keeping with this series'
"verify against real output" habit: the `'ORA-' in reinstate_result.stdout`
success check and the `+00 00:00:00` empty-lag match are both reasonable
guesses at 19c's actual output format, not confirmed against a real run —
expect to adjust either string once you see genuine `dgmgrl`/`sqlplus`
output. And Path A's restart is deliberately cancel-then-start rather than
a single idempotent command like MSSQL's `SET HADR RESUME` — issuing
`RECOVER MANAGED STANDBY DATABASE` while it's already running raises an
error, so this cancels first (harmless even if nothing was running) as the
Oracle equivalent of the same "make it safe to call twice" property.

### 2. New playbook: `ansible/playbooks/sync_rebuild.yml`

```yaml
---
- name: Resynchronize or reinstate a database in the Data Guard configuration
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run sync-rebuild tasks
      include_role:
        name: oracle_dg
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
    """Resynchronize or reinstate `target` ('vm3' or 'vm4') in the Data Guard configuration.

    Restarts managed recovery if the database is still a healthy broker
    member with stalled apply, or attempts REINSTATE DATABASE (via
    Flashback Database) if it fell out of the configuration entirely.
    """
    if target not in ("vm3", "vm4"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm3' or 'vm4'")

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
            "estimated_duration_minutes": 10,
        }
    except Exception as e:
        logger.error(f"Error initiating sync rebuild: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate sync rebuild: {str(e)}")
```

(`POST /deploy/validate` — used below for precheck/postcheck — already
exists from the build guide; nothing new to add for it here.)

### 5. Docs to update

- [`oracle-fastapi-build-from-scratch.md`](oracle-fastapi-build-from-scratch.md) —
  add Flashback Database to `primary_db.yml`/`standby_prep.yml` in a future
  revision (see Prerequisites above); until then, note it as a manual step
  in that guide's Prerequisites section too, so it isn't only documented
  here.
- Once `python-fastapi-oracle-dg/CHANGELOG.md`/`RUNBOOK.md` exist — add the
  `sync-rebuild` addition there: the two code paths (restart-apply vs.
  reinstate), why it branches on broker status rather than
  `v$database.database_role`, and whether the `REINSTATE` path has actually
  been exercised against a real forced-failover drill yet.

## Run it — CLI

**All-in-one** (the task file figures out which of the two broken states
applies and does the right thing):
```bash
curl -X POST "http://localhost:8001/api/v1/deploy/sync-rebuild?target=vm3"
tail -f logs/ansible.log
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'
```

**Stage by stage** — precheck, rebuild, postcheck, same three-phase shape
as the failover guide:
```bash
# 1. Precheck: confirm vm3 really is broken
curl -X POST http://localhost:8001/api/v1/deploy/validate
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0].results'

# 2. Rebuild
curl -X POST "http://localhost:8001/api/v1/deploy/sync-rebuild?target=vm3"
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'

# 3. Postcheck: confirm vm3 is PHYSICAL STANDBY / SUCCESS again
curl -X POST http://localhost:8001/api/v1/deploy/validate
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0].results'
```

**Full DR round-trip drill** (continuing the failover guide's drill, so you
practice the whole loop end to end):
```bash
# vm3 was shut down, vm4 was force-failed-over to primary (failover guide)
# bring vm3's instance back up first:
sudo -u oracle bash -c "\$ORACLE_HOME/bin/sqlplus -s / as sysdba <<'EOF'
STARTUP MOUNT;
EOF"

curl -X POST "http://localhost:8001/api/v1/deploy/sync-rebuild?target=vm3"
# vm3 is now PHYSICAL STANDBY again, synchronized to vm4

# optional: switch back to vm3 as primary once it's healthy
curl -X POST "http://localhost:8001/api/v1/deploy/switchover?target=vm3"
```

## Run it — Swagger UI

**All-in-one:**
1. Expand **POST /api/v1/deploy/sync-rebuild**, **Try it out**, fill
   `target=vm3`, **Execute**.
2. **GET /api/v1/deploy/history** → **Execute**, watch `status` go
   `running` → `success`/`failed`.

**Stage by stage:**
1. **POST /api/v1/deploy/validate** → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** to see vm3's broken state.
2. **POST /api/v1/deploy/sync-rebuild** with `target=vm3` → **Execute**.
3. **POST /api/v1/deploy/validate** → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** to confirm vm3 is
   `PHYSICAL STANDBY` / `SUCCESS`.

## Verification

```bash
# from either host, as the oracle user
dgmgrl sys/'<oracle_pwd>'@ORCL_S "SHOW CONFIGURATION VERBOSE"
```
Expect the rebuilt database's entry to show `SUCCESS`, role
`PHYSICAL STANDBY`, and `apply lag` at `0 seconds` in the detailed output.

## What could still go wrong (learning notes)

- **`REINSTATE` failing is a real dead end in this pass, not just a slow
  path.** Unlike MSSQL's Path B (automatic seeding always eventually
  works, it just takes longer for a bigger database), if `REINSTATE`
  fails here — flashback window exceeded, flashback logs never enabled,
  or the divergence is too large — this guide's automation has no
  fallback. The correct manual recovery is to treat the host as a fresh
  standby build: stop the instance, delete its datafiles/spfile/password
  file (the same cleanup `teardown.yml` already does for a standby), then
  re-duplicate it — but `standby_prep.yml`/`duplicate_standby.yml` (from
  the build guide) hard-code vm3-as-source/vm4-as-target, so rebuilding a
  **role-reversed** host (vm3 as standby, sourced from vm4) needs those
  two task files generalized to look up the live primary dynamically
  instead of assuming the original static mapping. Flagging this as a
  known gap rather than writing untested cross-host duplication logic to
  paper over it.
- **The Flashback Database retention window is the real clock you're
  racing**, not anything this task file checks. `REINSTATE` becomes
  impossible once `db_flashback_retention_target` (24 hours by default)
  has passed since the divergence — the longer a forced-failover drill sits
  before you run sync-rebuild, the more likely you'll hit the dead end
  above instead of a clean reinstate.
- **This doesn't handle true split-brain** (both databases independently
  believing they're primary, if the old primary comes back and someone
  opens it read-write before sync-rebuild runs). Always run
  `POST /deploy/validate` first and read the broker's actual status —
  `sync_rebuild.yml` trusts the broker's view of the target, which is only
  meaningful once the two sides can actually talk to each other again.
