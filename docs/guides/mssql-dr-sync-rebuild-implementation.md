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
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT ISNULL(drs.is_suspended, 0) FROM sys.databases d LEFT JOIN sys.dm_hadr_database_replica_states drs ON drs.database_id = d.database_id WHERE d.name = 'AdventureWorks'"
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
  shell: |
    {{ mssql_tools_path }}/sqlcmd -S localhost -U SA -P "{{ sa_password }}" -h -1 -W -Q "SET NOCOUNT ON; SELECT drs.synchronization_state_desc FROM sys.dm_hadr_database_replica_states drs JOIN sys.databases d ON d.database_id = drs.database_id WHERE d.name = 'AdventureWorks'"
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

### 5. Docs to update

- `python-fastapi-mssql/RUNBOOK.md` — add a "Sync rebuild" block under
  section 6, next to the `ag-status`/`failover` block from the failover
  guide, showing the `sync-rebuild` call and the precheck/postcheck pattern.
- `python-fastapi-mssql/CHANGELOG.md` — new entry describing the
  `sync-rebuild` addition: the two code paths (resume vs. rejoin-and-reseed),
  why it reuses `ag-status` instead of its own precheck, and whether it's
  been live-tested against the VMs yet (in particular, actually forcing a
  failover and rebuilding the old primary end to end) or only implemented.

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
