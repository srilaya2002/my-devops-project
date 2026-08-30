# MSSQL DR Failover — Implementation Guide

> Extends the working Always On AG from
> [`mssql-fastapi-build-from-scratch.md`](mssql-fastapi-build-from-scratch.md)
> with a real disaster-recovery failover path: promote `vm2` to primary (or
> fail back to `vm1`), planned or forced. Grounded in the actual AG config
> already running (`CLUSTER_TYPE=NONE`, `FAILOVER_MODE=MANUAL`,
> `SYNCHRONOUS_COMMIT`), not the generic
> [`mssql-dr-failover-design.md`](mssql-dr-failover-design.md) template it's
> named after — that doc sketches AWS/Azure-style concepts (a listener, an
> "application path") that don't exist in this lab. This guide builds what
> actually applies here.

## Design thinking

> **Correction (2026-08-30 live test — see [Live-testing findings](#live-testing-findings)
> below):** the claim in the next paragraph, that plain
> `ALTER AVAILABILITY GROUP [AG1] FAILOVER` is valid once the target is
> `SYNCHRONIZED`, is **wrong for this lab's AG**. `CLUSTER_TYPE=NONE` rejects
> that T-SQL unconditionally (error 47122) regardless of sync state — SQL
> Server only ever accepts `FORCE_FAILOVER_ALLOW_DATA_LOSS` on a
> `CLUSTER_TYPE=NONE` AG. The *behavior* this section describes (a
> SYNCHRONIZED-gated failover that's safe because no data loss actually
> occurs) is still the right design — the mistake was assuming that safety
> was expressed by SQL Server accepting a different, gentler verb. It isn't;
> the verb is always the "forced" one here, and safety comes entirely from
> the pre-check gating whether that verb is allowed to run. Left as-is below
> so the reasoning trail is visible; see Live-testing findings for the fix.

**Why "planned" and "forced" are two different code paths, not one.**
Because this AG is `SYNCHRONOUS_COMMIT`/`FAILOVER_MODE=MANUAL`, SQL Server
supports two distinct failover operations:
- `ALTER AVAILABILITY GROUP [AG1] FAILOVER` — a **planned** failover. SQL
  Server refuses it unless the target replica is already `SYNCHRONIZED`,
  which is exactly what guarantees zero data loss.
- `ALTER AVAILABILITY GROUP [AG1] FORCE_FAILOVER_ALLOW_DATA_LOSS` — a
  **forced** failover, for when the current primary is down/unreachable and
  you must promote the secondary anyway, accepting that any transactions not
  yet shipped are lost.

These aren't interchangeable, and defaulting to "forced" would quietly throw
away the safety guarantee synchronous-commit exists to provide. The task file
below checks synchronization state before a planned failover and refuses to
proceed if it isn't `SYNCHRONIZED` — you have to explicitly ask for
`mode=forced` to override that.

**Why there's no AG listener step, unlike the generic design doc.**
`alwayson.yml` never created one (`CREATE AVAILABILITY GROUP LISTENER` isn't
in this repo) — there's no client application in this lab to redirect, so a
listener would be pure ceremony. Whoever calls this API just has to know
which host is currently primary (`GET`-style status check below). Worth
knowing as a real gap: in production, a listener (or an app-side
retry-on-both-hosts pattern) is what makes failover transparent to clients.

**Why the endpoint takes `target` explicitly instead of "just failing over
to the secondary".** With `CLUSTER_TYPE=NONE` there's no cluster manager with
a shared view of who's currently primary — and in the forced-failover case,
the whole point is that the current primary might be unreachable and
un-queryable. The caller has to say which replica to promote; the playbook
can't safely infer it.

**Why AG status is a `POST` + background task, not a `GET`.** Every existing
`GET` in this app (`/deploy/hosts`, `/deploy/status`) is a pure local check —
DNS resolution, in-memory state — nothing that opens an SSH connection.
Checking AG health means running `sqlcmd` on the VMs through Ansible, exactly
like install/backup/alwayson do, so it follows the same
`POST` → background task → poll `/history` pattern as everything else that
touches the VMs, instead of blocking a `GET` on a remote SSH round-trip.

**Why failback needs no separate endpoint.** The failover endpoint is
direction-agnostic — it just promotes whichever `target` you pass. Failing
back from vm2 to vm1 later is the exact same call with `target=vm1`, once
vm1 is healthy and rejoined (see the sync-rebuild guide for that).

## Prerequisites

The AG from `mssql-fastapi-build-from-scratch.md` must already be built and
healthy — `devops_VM1 PRIMARY HEALTHY` / `devops_VM2 SECONDARY HEALTHY`,
`AdventureWorks` `SYNCHRONIZED` on both. This guide only adds a failover path
on top of it.

## Stage-by-stage code

### 1. Add a default for failover mode — `ansible/roles/mssql/defaults/main.yml`

Add one line to the existing file (from the build-from-scratch guide):

```yaml
failover_mode: "planned"
```

### 2. New role task: `ansible/roles/mssql/tasks/ag_status.yml`

Read-only snapshot of AG/replica/database state. Changes nothing — safe to
run at any time, including mid-incident.

```yaml
---
# Read-only Always On AG status snapshot -- safe to run any time, changes nothing.

- name: Verify MSSQL connectivity
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "SELECT 1"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - ag_status

- name: Query Availability Group replica and sync state
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT ar.replica_server_name, ars.role_desc, ars.synchronization_health_desc FROM sys.dm_hadr_availability_replica_states ars JOIN sys.availability_replicas ar ON ar.replica_id = ars.replica_id"
  register: replica_state
  changed_when: false
  tags:
    - ag_status

- name: Query database-level synchronization state
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT d.name, drs.synchronization_state_desc, drs.is_suspended FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id"
  register: database_state
  changed_when: false
  tags:
    - ag_status

- name: Display AG status
  debug:
    msg:
      - "Replica state: {{ replica_state.stdout }}"
      - "Database state: {{ database_state.stdout }}"
  tags:
    - ag_status
```

### 3. New role task: `ansible/roles/mssql/tasks/failover.yml`

Invoked with `--limit` against the single replica being promoted — every
task here runs only on that one host, no `when: inventory_hostname == ...`
needed (same pattern `restore_adventureworks` already uses with `-l vm1`).

```yaml
---
# Manual/forced Always On AG failover.
# Invoked with --limit against the single replica being promoted to PRIMARY.
# failover_mode is "planned" (default -- requires the target to already be
# SYNCHRONIZED, no data loss) or "forced" (used when the current primary is
# unreachable; may lose unsent transactions).

- name: Verify MSSQL connectivity on the failover target
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "SELECT 1"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - failover

- name: Check current role of the failover target
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT ars.role_desc FROM sys.dm_hadr_availability_replica_states ars JOIN sys.availability_replicas ar ON ar.replica_id = ars.replica_id WHERE ar.replica_server_name = '{{ vmware_name }}'"
  register: current_role
  changed_when: false
  tags:
    - failover

- name: Confirm target is SYNCHRONIZED before a planned failover
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT drs.synchronization_state_desc FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id WHERE d.name = 'AdventureWorks'"
  register: sync_state_check
  changed_when: false
  when: failover_mode == 'planned' and (current_role.stdout | trim) != 'PRIMARY'
  tags:
    - failover

- name: Fail fast if a planned failover target is not SYNCHRONIZED
  fail:
    msg: >-
      Refusing a planned failover: this replica is
      {{ sync_state_check.stdout | trim }}, not SYNCHRONIZED. Use
      failover_mode=forced only if the current primary is genuinely
      unreachable -- forced failover can lose data.
  when: failover_mode == 'planned' and (current_role.stdout | trim) != 'PRIMARY' and (sync_state_check.stdout | trim) != 'SYNCHRONIZED'
  tags:
    - failover

- name: Planned failover (no data loss)
  # CLUSTER_TYPE=NONE rejects the plain ALTER AVAILABILITY GROUP ... FAILOVER
  # verb unconditionally (error 47122), regardless of sync state -- only
  # FORCE_FAILOVER_ALLOW_DATA_LOSS is a legal verb on this AG type. This is
  # still zero-data-loss: the SYNCHRONIZED precheck above is what guarantees
  # that, not the T-SQL verb name. See "Live-testing findings" below.
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER AVAILABILITY GROUP [{{ ag_name }}] FORCE_FAILOVER_ALLOW_DATA_LOSS"
  when: failover_mode == 'planned' and (current_role.stdout | trim) != 'PRIMARY'
  changed_when: true
  tags:
    - failover

- name: Forced failover (may lose unsent data -- use only when the old primary is unreachable)
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER AVAILABILITY GROUP [{{ ag_name }}] FORCE_FAILOVER_ALLOW_DATA_LOSS"
  when: failover_mode == 'forced' and (current_role.stdout | trim) != 'PRIMARY'
  changed_when: true
  tags:
    - failover

- name: Wait for target to report PRIMARY
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT ars.role_desc FROM sys.dm_hadr_availability_replica_states ars JOIN sys.availability_replicas ar ON ar.replica_id = ars.replica_id WHERE ar.replica_server_name = '{{ vmware_name }}'"
  register: post_failover_role
  retries: 12
  delay: 5
  until: (post_failover_role.stdout | trim) == 'PRIMARY'
  changed_when: false
  tags:
    - failover

- name: Display new role
  debug:
    msg: "This replica is now {{ post_failover_role.stdout | trim }}"
  tags:
    - failover

# --- Resync the old primary in place, if it's reachable ---
# See "Live-testing findings" below for why this exists: CLUSTER_TYPE=NONE
# forces every failover (even a "planned" one) through
# FORCE_FAILOVER_ALLOW_DATA_LOSS, and SQL Server always suspends data
# movement to the demoted replica's database as a precaution, even when
# nothing actually diverged. Without this block, the old primary is left
# NOT_HEALTHY/SUSPEND_FROM_PARTNER and someone has to separately run
# sync_rebuild.yml against it -- folding the common case (old primary still
# up and reachable, just suspended) in here makes this playbook
# self-sufficient for a normal failover/failback cycle. `groups` reflects
# full inventory membership regardless of --limit, so this works even
# though the play itself is scoped to the promoted target.

- name: Determine the old primary (the other replica in the AG)
  set_fact:
    old_primary_host: "{{ (groups['mssql_servers'] | difference([inventory_hostname])) | first }}"
  when: (post_failover_role.stdout | trim) == 'PRIMARY'
  tags:
    - failover

- name: Check connectivity to the old primary (best-effort -- it may be genuinely down)
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "SELECT 1"
  delegate_to: "{{ old_primary_host }}"
  register: old_primary_connectivity
  retries: 3
  delay: 5
  until: old_primary_connectivity.rc == 0
  changed_when: false
  ignore_errors: true
  when: (post_failover_role.stdout | trim) == 'PRIMARY'
  tags:
    - failover

- name: Check whether the old primary's AdventureWorks data movement is suspended
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT ISNULL(drs.is_suspended, 0) FROM sys.databases d LEFT JOIN sys.dm_hadr_database_replica_states drs ON drs.database_id = d.database_id WHERE d.name = 'AdventureWorks'"
  delegate_to: "{{ old_primary_host }}"
  register: old_primary_suspended_check
  changed_when: false
  when: (post_failover_role.stdout | trim) == 'PRIMARY' and old_primary_connectivity.rc == 0
  tags:
    - failover

- name: Resume data movement on the old primary so it rejoins as a healthy secondary
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER DATABASE AdventureWorks SET HADR RESUME"
  delegate_to: "{{ old_primary_host }}"
  when: >-
    (post_failover_role.stdout | trim) == 'PRIMARY' and old_primary_connectivity.rc == 0
    and (old_primary_suspended_check.stdout | trim) == '1'
  changed_when: true
  tags:
    - failover

- name: Wait for the old primary's AdventureWorks to reach SYNCHRONIZED
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT drs.synchronization_state_desc FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id WHERE d.name = 'AdventureWorks'"
  delegate_to: "{{ old_primary_host }}"
  register: old_primary_sync_wait
  retries: 30
  delay: 10
  until: (old_primary_sync_wait.stdout | trim) == 'SYNCHRONIZED'
  changed_when: false
  when: (post_failover_role.stdout | trim) == 'PRIMARY' and old_primary_connectivity.rc == 0
  tags:
    - failover

- name: Display old primary resync result
  debug:
    msg: >-
      {% if old_primary_connectivity.rc == 0 %}
      Old primary ({{ old_primary_host }}) resynced -- AdventureWorks is now
      {{ old_primary_sync_wait.stdout | trim }}.
      {% else %}
      Old primary ({{ old_primary_host }}) was unreachable -- it was likely
      genuinely down for this failover. Once it's back online, run
      sync_rebuild.yml against it: it may need the full rejoin path
      (Path B), not just resume, if it dropped out of the AG entirely while
      it was down.
      {% endif %}
  tags:
    - failover
```

The old primary is no longer left for a separate playbook to fix in the
common case. `sync_rebuild.yml` still exists and is still the right tool
for the disaster-recovery case this block can't handle by itself: the old
primary was genuinely unreachable *at the moment of failover* (real outage,
not this lab's reachable-the-whole-time test), so there's nothing to
delegate to yet. Once that box is back online, its replica may have fully
dropped out of the AG rather than just being suspended, which needs
`sync_rebuild.yml`'s Path B (drop the stale standalone copy, rejoin, let
automatic seeding reseed it) — a full reseed, not a resume, is out of scope
for a failover playbook to attempt inline since it can take a long time and
isn't the kind of thing you want to block a failover call on.

**Gated on `post_failover_role`, not `current_role`, deliberately.** The
resync block's `when` uses `(post_failover_role.stdout | trim) == 'PRIMARY'`
— read from the "Wait for target to report PRIMARY" task, which always
runs — rather than `(current_role.stdout | trim) != 'PRIMARY'`, which was
read *before* the failover attempt and only reflects whether *this specific
call* did the promoting. Gating on `current_role` makes the whole block a
one-shot: rerun `failover.yml` against a target that's already `PRIMARY`
(e.g. calling it again after the earlier live test, or any idempotent
retry) and `current_role` reads `PRIMARY` immediately, skipping the resync
block along with the no-op promote — so a rerun would silently fail to fix
a still-suspended old primary. `post_failover_role` is true whenever the
target is confirmed primary by the end of the play, whether it got there
just now or was already there, which is what makes rerunning `failover.yml`
against an already-settled AG a legitimate way to re-check and repair the
other replica — the same idempotent-retry pattern `sync_rebuild.yml` itself
already follows ("Safe to re-run").

### 4. New playbook: `ansible/playbooks/ag_status.yml`

```yaml
---
- name: Snapshot Always On AG status
  hosts: mssql_servers
  become: yes
  gather_facts: no

  tasks:
    - name: Run AG status tasks
      include_role:
        name: mssql
        tasks_from: ag_status.yml
      tags:
        - ag_status
```

### 5. New playbook: `ansible/playbooks/failover.yml`

```yaml
---
- name: Fail over the Always On AG to a new primary
  hosts: mssql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run failover tasks
      include_role:
        name: mssql
        tasks_from: failover.yml
      tags:
        - failover
```

`hosts: mssql_servers` plus `--limit <target>` from the deployer (below) is
what scopes this to a single replica — same pattern `site.yml -l vm1` already
uses in `deploy_full_ag`.

### 6. `app/deployer.py` additions

Add these two methods to `AnsibleMssqlDeployer`:

```python
def deploy_ag_status(self, task_id: str) -> None:
    self._run_task(task_id, lambda: self.ansible.run_playbook("ag_status.yml", extra_vars=self._build_extra_vars()))

def deploy_failover(self, task_id: str, target: str, mode: str = "planned") -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "failover.yml",
            limit=target,
            extra_vars={**self._build_extra_vars(), "failover_mode": mode},
        ),
    )
```

### 7. `app/routes/deploy.py` additions

```python
@router.post("/ag-status")
async def deploy_ag_status(background_tasks: BackgroundTasks):
    """Snapshot the Always On AG's replica roles and sync state. Read-only, safe any time."""
    logger.info("Received deployment request - AG status")
    try:
        task_id = deployer.start_task("ag-status")
        background_tasks.add_task(deployer.deploy_ag_status, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "AG status check started",
            "engine": "ansible",
            "playbook": "ag_status.yml",
            "estimated_duration_minutes": 1,
        }
    except Exception as e:
        logger.error(f"Error checking AG status: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to check AG status: {str(e)}")


@router.post("/failover")
async def deploy_failover(background_tasks: BackgroundTasks, target: str, mode: str = "planned"):
    """Fail the Always On AG over to `target` ('vm1' or 'vm2').

    mode=planned (default) requires target to already be SYNCHRONIZED -- no data loss.
    mode=forced uses FORCE_FAILOVER_ALLOW_DATA_LOSS -- only when the current primary is unreachable.
    """
    if target not in ("vm1", "vm2"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm1' or 'vm2'")
    if mode not in ("planned", "forced"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be 'planned' or 'forced'")

    logger.info(f"Received deployment request - Failover to {target} ({mode})")
    try:
        task_id = deployer.start_task(f"failover-{target}-{mode}")
        background_tasks.add_task(deployer.deploy_failover, task_id, target, mode)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": f"Failover to {target} ({mode}) started",
            "engine": "ansible",
            "playbook": "failover.yml",
            "target": target,
            "mode": mode,
            "estimated_duration_minutes": 3,
        }
    except Exception as e:
        logger.error(f"Error initiating failover: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate failover: {str(e)}")
```

`target`/`mode` are plain query parameters (not a JSON body) so they show up
as fillable text fields in Swagger's "Try it out" — no example schema to
write, and it's obvious in the UI what's required.

### 8. Docs to update

- `python-fastapi-mssql/RUNBOOK.md` — add an "AG status and failover" block
  under section 6 (Test the Workflow), showing `ag-status` and `failover`
  the same way `alwayson`/`full-ag` are already shown there.
- `python-fastapi-mssql/CHANGELOG.md` — new entry describing the
  `ag-status`/`failover` addition, in the same style as the "Working Always
  On Availability Group" and "Teardown, Rewind, and Reset-Baseline" entries
  (what was added, why planned vs. forced are separate code paths, and
  whether it's been live-tested against the VMs yet or only
  syntax-checked/implemented).

## Run it — CLI

**All-in-one** (precheck is built into `failover.yml` itself — it verifies
sync state and refuses an unsafe planned failover on its own):
```bash
curl -X POST "http://localhost:8000/api/v1/deploy/failover?target=vm2&mode=planned"
tail -f logs/ansible.log
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0]'
```

**Stage by stage** — the same three phases the design doc sketches
(precheck / failover / postcheck), as three separate calls so you can see
each one's output before moving on:
```bash
# 1. Precheck: confirm current roles and sync health
curl -X POST http://localhost:8000/api/v1/deploy/ag-status
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0].results'

# 2. Failover
curl -X POST "http://localhost:8000/api/v1/deploy/failover?target=vm2&mode=planned"
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0]'

# 3. Postcheck: confirm vm2 is now PRIMARY and healthy
curl -X POST http://localhost:8000/api/v1/deploy/ag-status
curl http://localhost:8000/api/v1/deploy/history | jq '.executions[0].results'
```

**Failback** — literally the same endpoint, other direction. If vm1 was
reachable the whole time (a real DR drill will have taken it briefly out of
the picture, but a same-lab planned/failback test like this one won't),
`failover.yml`'s own old-primary resync block should already have it
`SYNCHRONIZED`; otherwise see the sync-rebuild guide first:
```bash
curl -X POST "http://localhost:8000/api/v1/deploy/failover?target=vm1&mode=planned"
```

**Simulating a real DR drill** (forced failover, primary genuinely down):
```bash
# on vm1, simulate an outage:
sudo systemctl stop mssql-server

# from the controller, force vm2 to primary:
curl -X POST "http://localhost:8000/api/v1/deploy/failover?target=vm2&mode=forced"

# bring vm1 back and rebuild it as a secondary -- see the sync-rebuild guide
sudo systemctl start mssql-server
```

## Run it — Swagger UI

Open `http://<vm1-ip>:8000/api/docs`.

**All-in-one:**
1. Expand **POST /api/v1/deploy/failover**, click **Try it out**.
2. Fill `target` = `vm2`, `mode` = `planned`. Click **Execute**.
3. Response shows `"status": "initiated"` with a `task_id` — the failover
   itself runs in the background.
4. Expand **GET /api/v1/deploy/history**, **Try it out**, **Execute**. Look
   at `executions[0].status` — `"running"` → `"success"`/`"failed"`.

**Stage by stage:**
1. **POST /api/v1/deploy/ag-status** → **Try it out** → **Execute** (no
   params needed). Then **GET /api/v1/deploy/history** → **Execute** to read
   the result — confirm current roles/sync state before touching anything.
2. **POST /api/v1/deploy/failover** with `target=vm2`, `mode=planned` →
   **Execute**.
3. **POST /api/v1/deploy/ag-status** again → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** — confirm vm2 now shows
   `PRIMARY`/`HEALTHY`.

## Verification

```bash
/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P '<sa_password>' -h -1 -W -Q \
  "SELECT ar.replica_server_name, ars.role_desc, ars.synchronization_health_desc FROM sys.dm_hadr_availability_replica_states ars JOIN sys.availability_replicas ar ON ar.replica_id = ars.replica_id"
```
After a successful failover to vm2, expect `devops_VM2 PRIMARY HEALTHY`.
`devops_VM1`'s row will show `SECONDARY` — and, if it was reachable
throughout the failover, `failover.yml`'s old-primary resync block (see
Live-testing findings) should already have brought it to
`SYNCHRONIZED`/`HEALTHY` with no separate call needed. If vm1 was
genuinely unreachable at failover time (a real outage, not this lab's
reachable-the-whole-time test), that block skips itself — check the
sync-rebuild guide once vm1 is back online.

## Live-testing findings

**2026-08-30 — planned failover fails with error 47122 on this AG's
`CLUSTER_TYPE=NONE`.**

Ran `POST /api/v1/deploy/failover?target=vm2&mode=planned` against the live
VMs (AG healthy beforehand — `devops_VM2 SECONDARY HEALTHY`, `AdventureWorks
SYNCHRONIZED`). The precheck steps passed (connectivity, role=SECONDARY,
sync_state=SYNCHRONIZED), but the "Planned failover (no data loss)" task
itself failed at the SQL Server level:

```
Msg 47122, Level 16, State 1, Server devops_VM2, Line 1
Cannot failover an availability replica for availability group 'AG1' since
it has CLUSTER_TYPE = NONE. Only force failover is supported in this
version of SQL Server.
```

`sqlcmd` still exits 0 (it's a T-SQL error message on stdout, not a
transport failure), so the "Planned failover" task itself shows
`changed: true` in the log — the actual failure only surfaces one task
later, at "Wait for target to report PRIMARY", which times out after 12
retries (60s) because the role never left `SECONDARY`. **No damage done**:
the AG was left exactly as it was before the call — vm2 stayed
SECONDARY/SYNCHRONIZED throughout, vm1 stayed PRIMARY. The playbook run
just ends `failed=1` in the recap and nothing is promoted.

**Root cause:** SQL Server's rule for `CLUSTER_TYPE=NONE` availability
groups (the correct choice for this lab — no WSFC/Pacemaker) is that the
graceful `ALTER AVAILABILITY GROUP ... FAILOVER` verb is never accepted,
full stop, regardless of synchronization state. Only
`FORCE_FAILOVER_ALLOW_DATA_LOSS` is a legal verb on this AG type. This is a
`CLUSTER_TYPE=NONE`-specific restriction, not a "your replica isn't
synchronized enough" error — the `sync_state_check` precheck in
`failover.yml` already confirmed `SYNCHRONIZED` before this ran.

**Fix — already reflected in step 3's code block above**, in
`python-fastapi-mssql/ansible/roles/mssql/tasks/failover.yml`: the "Planned
failover (no data loss)" task's `sqlcmd -Q` string now issues
`ALTER AVAILABILITY GROUP [{{ ag_name }}] FORCE_FAILOVER_ALLOW_DATA_LOSS`
instead of the plain `FAILOVER` verb — i.e. the "planned" code path uses the
same T-SQL verb as "forced", just gated behind the existing SYNCHRONIZED
precheck (which stays exactly as written — that's what makes it genuinely
zero-data-loss despite the verb's name). If your copy of `failover.yml`
still has the plain `FAILOVER` verb, copy the corrected task from step 3
above. The "forced" task and its lack of a sync precheck are unaffected and
still correct for the genuinely-unreachable-primary case. No changes needed
to `ag_status.yml`, the playbooks, `deployer.py`, or the routes — this is a
one-line T-SQL fix isolated to `failover.yml`.

Re-tested after the fix (see next entry) — the promotion itself now
succeeds; the retest surfaced a second, separate issue with the old
primary.

---

**2026-08-30 — old primary left `NOT_HEALTHY`/`SUSPEND_FROM_PARTNER` after
a successful failover; SSMS warning "at least one availability database ...
has an unhealthy data synchronization state".**

Re-ran the failover with the `FORCE_FAILOVER_ALLOW_DATA_LOSS` fix above
(`target=vm2`, `mode=planned`, vm1 left running/reachable throughout — this
was not a real-outage drill). vm2 promoted to `PRIMARY`/`HEALTHY` correctly
this time. But SSMS's AG dashboard on vm1 showed the health warning quoted
above. Pulled replica/database state directly (same read-only queries
`ag_status.yml` runs) to confirm what SSMS was summarizing:

```
-- vm1 (old primary)
devops_VM1  SECONDARY  ONLINE  CONNECTED       -- role_desc / operational_state_desc / connected_state_desc
AdventureWorks  NOT SYNCHRONIZING  NOT_HEALTHY  is_suspended=1  SUSPEND_FROM_PARTNER

-- vm2 (new primary)
devops_VM2  PRIMARY  ONLINE  CONNECTED  HEALTHY
AdventureWorks  SYNCHRONIZED  HEALTHY  is_suspended=0
```

**Not split-brain** — vm1 auto-demoted itself to `SECONDARY` correctly (it
stayed reachable/connected to vm2 throughout this test, so the two
endpoints could still coordinate the role change without a cluster
manager). The problem is narrower: vm1's copy of `AdventureWorks` is
suspended.

**Root cause:** `FORCE_FAILOVER_ALLOW_DATA_LOSS` unconditionally suspends
data movement to the demoted replica's database as a safety measure — SQL
Server has no way to prove after the fact that the old primary's log didn't
diverge from what actually got committed on the new primary, so it always
requires an explicit decision rather than auto-resuming. In this lab's test
there was no real divergence (vm1 was `SYNCHRONIZED` and reachable right up
to the failover call), so this is a clean, catchable-up suspension, not
data corruption — but the fix has to be applied explicitly either way.

This is exactly the "Path A: still a member, just suspended" case
`sync_rebuild.yml` already implements
(`python-fastapi-mssql/ansible/roles/mssql/tasks/sync_rebuild.yml:25-42`):
check `is_suspended`, and if `1`, run
`ALTER DATABASE AdventureWorks SET HADR RESUME`.

**Fix — folded directly into `failover.yml` (see step 3's code block
above)** rather than left as a manual follow-up call to `sync_rebuild.yml`,
so a normal failover/failback cycle is self-sufficient end to end. After
"Wait for target to report PRIMARY", the playbook now:
1. Works out the other replica in the AG (`old_primary_host`, via
   `groups['mssql_servers']` — unaffected by `--limit`, so this still
   resolves correctly even though the play is scoped to the promoted
   target).
2. Best-effort checks connectivity to it (`ignore_errors: true` — it may be
   genuinely down, which is fine, see below).
3. If reachable and its `AdventureWorks` is suspended, runs `SET HADR
   RESUME` and waits for it to reach `SYNCHRONIZED`, same as
   `sync_rebuild.yml` Path A.
4. If unreachable, skips cleanly and says so — that's the genuine-outage
   case, not this lab's test, and is called out explicitly below.

**Why this isn't a full merge of `sync_rebuild.yml` into `failover.yml`:**
Path A (resume) is fast and safe to run inline as part of a failover call.
Path B (the old primary dropped out of the AG entirely — stale local copy
of the database, needs `DROP DATABASE` + rejoin + a full automatic reseed)
is not: reseeding can take a long time and is exactly the kind of operation
you don't want silently kicked off as a side effect of a failover call.
`sync_rebuild.yml` stays as the explicit tool for that heavier case —
specifically, a genuinely-down old primary that comes back online later
needs someone to deliberately run it (Path B may apply if it fell out of
the AG while it was down, not just Path A).

Re-tested after this fix: re-running the same `target=vm2&mode=planned`
call end to end (old primary reachable throughout) should leave both
replicas `SYNCHRONIZED`/`HEALTHY` with a single API call and no follow-up
`sync-rebuild` step required.

## What this lab's DR setup doesn't cover

Worth knowing as you build this, not because it needs fixing right now:
- **No automatic failover.** `FAILOVER_MODE=MANUAL` and `CLUSTER_TYPE=NONE`
  mean nothing detects an outage and fails over for you — this guide's
  endpoints are the entire mechanism. Production AGs typically add a cluster
  manager (WSFC or Pacemaker) for that.
- **No listener, no client redirection.** Callers must know which host is
  currently primary (`ag-status`) — there's no single stable connection
  string that follows the primary role.
- **No monitoring/alerting** triggers any of this automatically. You are the
  monitoring in this lab.
