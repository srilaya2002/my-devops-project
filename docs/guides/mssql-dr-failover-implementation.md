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
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -Q "ALTER AVAILABILITY GROUP [{{ ag_name }}] FAILOVER"
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
```

Note this deliberately does **not** try to do anything to the old primary —
for a planned failover, SQL Server demotes it automatically as part of the
role change; for a forced failover, it's typically unreachable anyway. Its
recovery is the sync-rebuild guide's job, not this one's.

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

**Failback** — literally the same endpoint, other direction, once vm1 is
healthy and rejoined (see the sync-rebuild guide if it isn't):
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
After a successful planned failover to vm2, expect `devops_VM2 PRIMARY
HEALTHY`. `devops_VM1`'s row will show `SECONDARY` — but check the
sync-rebuild guide before trusting its health after a **forced** failover;
that's exactly the scenario it exists to fix.

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
