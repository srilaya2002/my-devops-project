# MySQL Replication Sync Rebuild — Implementation Guide

> The reverse of
> [`mysql-replication-failover-implementation.md`](mysql-replication-failover-implementation.md):
> bring a host that's fallen behind — or dropped out of replication
> entirely — back to a healthy, synchronized replica. Grounded in the same
> live GTID pair this whole series builds on, not the generic
> [`mysql-replication-sync-rebuild-design.md`](mysql-replication-sync-rebuild-design.md)
> template, which defaults to "take a fresh backup and restore it" as the
> only rebuild path. This guide does that too, as the fallback — but leads
> with something cheaper GTID replication makes possible that the generic
> template doesn't mention: reconnecting a diverged-but-not-really-diverged
> host without moving any data at all. Read the failover guide first — this
> one reuses its `POST /deploy/repl-status` endpoint for precheck/postcheck
> rather than redefining it, the same relationship
> [`mssql-dr-sync-rebuild-implementation.md`](mssql-dr-sync-rebuild-implementation.md)
> and
> [`oracle-dataguard-sync-rebuild-implementation.md`](oracle-dataguard-sync-rebuild-implementation.md)
> have to their own failover guides.
>
> Same caveat as the rest of this series: **a plan to review, not a record
> of a working build** — nothing here has run against `devops_VM5`/
> `devops_VM6` yet.

## Design thinking

**Two genuinely different broken states, detected the MySQL-native way.**
"Out of sync" is two problems here too, same shape as the other two DR
series:

1. **Still configured as a replica, but the threads stopped** — a network
   blip, a restart that didn't bring `START REPLICA` back with it, a
   transient error that killed the SQL or IO thread. Fix: `START REPLICA`
   again. Cheap, no data movement beyond catching up whatever's already in
   the relay log.
2. **Not configured as a replica at all** — the standard case is the **old
   primary after a forced failover**: `failover.yml` deliberately never
   touches the old primary, so once it's reachable again it's just sitting
   there as a standalone writable server with no `CHANGE REPLICATION
   SOURCE` configuration and possibly a few transactions the new primary
   never got.

`SHOW REPLICA STATUS` returning empty is this guide's membership check —
the direct MySQL equivalent of MSSQL's `COUNT(*) FROM
sys.availability_replicas` and Oracle's broker `SHOW DATABASE` status,
just simpler: there's no broker tracking membership separately from the
replica's own configuration, so the configuration itself *is* the
membership record.

**Why `GTID_SUBTRACT` is the actual mechanism this whole guide hangs off
of.** Before reconnecting a database that fell out of replication, you need
to answer one question: does it know about anything the current primary
doesn't? MySQL has a built-in, deterministic answer —
`SELECT GTID_SUBTRACT(<this host's gtid_executed>, <primary's
gtid_executed>)` returns exactly the set of transactions this host has that
the primary has never seen. Empty result: nothing to lose, safe to
reconnect with `SOURCE_AUTO_POSITION=1` and no data movement at all — the
direct MySQL analog to Oracle's `REINSTATE DATABASE`, and actually more
certain than it: Oracle's flashback-based reinstate can still fail if the
retention window has passed, but `GTID_SUBTRACT` gives a yes/no answer
right now, with no time pressure attached. Non-empty result: real
divergence, and this guide draws the same line Oracle's does — see "What
could still go wrong" below for what's not automated past that point.

**Why the reconnect path also sets `read_only`, closing the failover
guide's flagged gap a second time.** The failover guide already noted
`my.cnf.j2` never turns `read_only` on for the replica. This guide's
Path B reconnect step sets `SET GLOBAL read_only = ON; SET GLOBAL
super_read_only = ON;` as part of reconnecting, on the same defense-in-depth
logic — a host being rebuilt as a replica shouldn't be silently writable in
the meantime, regardless of whether the underlying config gap has been
fixed yet.

**Why full re-clone with reversed source/target isn't implemented here.**
The generic design doc's whole rebuild path is "take a fresh XtraBackup
snapshot and restore it" — exactly what `backup.yml`/`restore.yml` already
do for the *initial* build. The problem is the same one the Oracle guide
ran into: those task files hard-code vm5-as-source/vm6-as-target, so
reusing them to rebuild a **role-reversed** vm5 (as a replica, sourced from
vm6) needs them generalized to look up the live primary dynamically first.
Rather than write that untested generalization here, this guide implements
the cheap `GTID_SUBTRACT` path fully (which covers the common case — most
forced failovers lose at most a few seconds of transactions, and a clean
crash often loses none) and documents the divergent case as a known gap,
the same call the Oracle guide made for the same underlying reason.

**Why it reuses `POST /deploy/repl-status` rather than defining its own
precheck.** Same reasoning as the other two guides: "is this host healthy?"
doesn't change between the failover and sync-rebuild guides — reuse it.

**Why `sync_rebuild.yml` runs against both hosts, like `switchover.yml`,
not `--limit <target>`.** The divergence check needs to compare the broken
host's GTID set against the *live* primary's — which means both hosts need
to be in the same play, the same reasoning `switchover.yml` already
established for needing both sides present at once.

## Prerequisites

- The replication pair from `mysql-fastapi-build-from-scratch.md`, built
  and (at some point) healthy — confirm with `POST /deploy/repl-status`.
- A replica that actually needs rebuilding — the easiest way to get one on
  purpose, for practicing this: follow the failover guide's forced-failover
  drill (shut down vm5, then `POST /deploy/failover?target=vm6`, then bring
  vm5 back up). vm5 is now the broken host this guide fixes.

## Stage-by-stage code

### 1. New role task: `ansible/roles/mysql_repl/tasks/sync_rebuild.yml`

Runs against both hosts in one play, same shape as `switchover.yml` —
receives `rebuild_target` as an extra-var naming the broken host; the other
host is queried live to find the current primary.

```yaml
---
# Resynchronize or reconnect a host that has fallen behind or dropped out
# of replication entirely. Runs against both hosts; rebuild_target
# (extra-var) names the broken host being rebuilt.

- name: Verify MySQL connectivity
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT 1;"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - sync_rebuild

- name: Get this host's GTID-executed set (needed by both hosts for the divergence check)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT @@GLOBAL.gtid_executed;" -N -B
  register: gtid_executed_check
  changed_when: false
  tags:
    - sync_rebuild

- name: Determine the live primary (the other host in the pair)
  set_fact:
    live_primary_host: "{{ (groups['mysql_servers'] | difference([rebuild_target]))[0] }}"
  tags:
    - sync_rebuild

- name: Check the rebuild target's current replica configuration
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G"
  register: replica_status_check
  changed_when: false
  when: inventory_hostname == rebuild_target
  tags:
    - sync_rebuild

# --- Path A: still configured, threads just stopped ---

- name: Restart replica threads (still configured, just stopped)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "START REPLICA;"
  when: inventory_hostname == rebuild_target and (replica_status_check.stdout | trim) != ''
  changed_when: true
  tags:
    - sync_rebuild

# --- Path B: not configured at all -- check for divergence, then reconnect ---

- name: Check the target for GTIDs the live primary has never seen (divergence)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    SELECT GTID_SUBTRACT('{{ gtid_executed_check.stdout }}', '{{ hostvars[live_primary_host].gtid_executed_check.stdout }}');
    " -N -B
  register: divergence_check
  when: inventory_hostname == rebuild_target and (replica_status_check.stdout | trim) == ''
  changed_when: false
  tags:
    - sync_rebuild

- name: Fail clearly if the target has diverged from the live primary
  fail:
    msg: >-
      {{ rebuild_target }} has committed transactions the current primary
      ({{ live_primary_host }}) has never seen (GTID_SUBTRACT returned
      "{{ divergence_check.stdout }}"). This guide's automation stops here
      -- a full re-clone with reversed source/target isn't implemented yet,
      see "What could still go wrong" in this doc for the manual fallback.
  when: inventory_hostname == rebuild_target and (replica_status_check.stdout | trim) == '' and (divergence_check.stdout | trim) != ''
  tags:
    - sync_rebuild

- name: Reconnect the target to the live primary (no divergence -- safe to auto-position)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    CHANGE REPLICATION SOURCE TO
      SOURCE_HOST='{{ hostvars[live_primary_host].ansible_host }}',
      SOURCE_PORT={{ mysql_port }},
      SOURCE_USER='{{ mysql_repl_user }}',
      SOURCE_PASSWORD='{{ mysql_repl_password }}',
      SOURCE_AUTO_POSITION=1;
    SET GLOBAL read_only = ON;
    SET GLOBAL super_read_only = ON;
    START REPLICA;
    "
  when: inventory_hostname == rebuild_target and (replica_status_check.stdout | trim) == '' and (divergence_check.stdout | trim) == ''
  changed_when: true
  tags:
    - sync_rebuild

# --- Verify, either path ---

- name: Wait for replica threads to report healthy
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running"
  register: final_replica_check
  retries: 30
  delay: 10
  until: "'Replica_IO_Running: Yes' in final_replica_check.stdout and 'Replica_SQL_Running: Yes' in final_replica_check.stdout"
  when: inventory_hostname == rebuild_target
  changed_when: false
  tags:
    - sync_rebuild

- name: Display final replica status
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G"
  register: final_status_display
  when: inventory_hostname == rebuild_target
  changed_when: false
  tags:
    - sync_rebuild

- name: Show result
  debug:
    msg: "{{ final_status_display.stdout_lines }}"
  when: inventory_hostname == rebuild_target
  tags:
    - sync_rebuild
```

The `-N -B` flags on the GTID queries (`-N` skips column headers, `-B`
gives tab-separated batch output) keep `gtid_executed_check.stdout` a clean
string with nothing to strip before it's reused inside another SQL
statement — worth double-checking on the first real run that a
multi-source GTID set (comma-separated UUID:ranges) round-trips cleanly
through the shell quoting here, the same "verify against real output"
caveat as everywhere else uncertain in this series.

### 2. New playbook: `ansible/playbooks/sync_rebuild.yml`

```yaml
---
- name: Resynchronize or reconnect a MySQL host to replication
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run sync-rebuild tasks
      include_role:
        name: mysql_repl
        tasks_from: sync_rebuild.yml
      tags:
        - sync_rebuild
```

Runs against the whole group, no `--limit` — same reasoning as
`switchover.yml`.

### 3. `app/deployer.py` addition

```python
def deploy_sync_rebuild(self, task_id: str, target: str) -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "sync_rebuild.yml",
            extra_vars={**self._build_extra_vars(), "rebuild_target": target},
        ),
    )
```

### 4. `app/routes/deploy.py` addition

```python
@router.post("/sync-rebuild")
async def deploy_sync_rebuild(background_tasks: BackgroundTasks, target: str):
    """Resynchronize or reconnect `target` ('vm5' or 'vm6') to replication.

    Restarts replica threads if still configured and just stopped, or
    checks for GTID divergence against the live primary and reconnects
    with SOURCE_AUTO_POSITION=1 if none is found.
    """
    if target not in ("vm5", "vm6"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm5' or 'vm6'")

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
            "estimated_duration_minutes": 5,
        }
    except Exception as e:
        logger.error(f"Error initiating sync rebuild: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate sync rebuild: {str(e)}")
```

(`POST /deploy/repl-status` — used below for precheck/postcheck — already
exists from the failover guide; nothing new to add for it here.)

### 5. Docs to update

- [`mysql-fastapi-build-from-scratch.md`](mysql-fastapi-build-from-scratch.md) —
  the `read_only`/`SET PERSIST` follow-up flagged in the failover guide's
  Design thinking still applies here too; no new gap to add on top of it.
- Once `python-fastapi-mysql/CHANGELOG.md`/`RUNBOOK.md` exist — add the
  `sync-rebuild` addition there: the two code paths (restart vs. reconnect),
  why it branches on `GTID_SUBTRACT` rather than a membership count, and
  whether the reconnect path has actually been exercised against a real
  forced-failover drill yet.

## Run it — CLI

**All-in-one** (the task file figures out which of the two broken states
applies and does the right thing):
```bash
curl -X POST "http://localhost:8002/api/v1/deploy/sync-rebuild?target=vm5"
tail -f logs/ansible.log
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'
```

**Stage by stage** — precheck, rebuild, postcheck, same three-phase shape
as the failover guide:
```bash
# 1. Precheck: confirm vm5 really is broken
curl -X POST http://localhost:8002/api/v1/deploy/repl-status
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0].results'

# 2. Rebuild
curl -X POST "http://localhost:8002/api/v1/deploy/sync-rebuild?target=vm5"
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'

# 3. Postcheck: confirm vm5 is a healthy replica again
curl -X POST http://localhost:8002/api/v1/deploy/repl-status
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0].results'
```

**Full DR round-trip drill** (continuing the failover guide's drill, so you
practice the whole loop end to end):
```bash
# vm5 was shut down, vm6 was force-failed-over to primary (failover guide)
sudo systemctl start mysqld   # on vm5

curl -X POST "http://localhost:8002/api/v1/deploy/sync-rebuild?target=vm5"
# vm5 is now a healthy replica again, synchronized to vm6

# optional: switch back to vm5 as primary once it's healthy
curl -X POST "http://localhost:8002/api/v1/deploy/switchover?target=vm5"
```

## Run it — Swagger UI

**All-in-one:**
1. Expand **POST /api/v1/deploy/sync-rebuild**, **Try it out**, fill
   `target=vm5`, **Execute**.
2. **GET /api/v1/deploy/history** → **Execute**, watch `status` go
   `running` → `success`/`failed`.

**Stage by stage:**
1. **POST /api/v1/deploy/repl-status** → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** to see vm5's broken state.
2. **POST /api/v1/deploy/sync-rebuild** with `target=vm5` → **Execute**.
3. **POST /api/v1/deploy/repl-status** → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** to confirm vm5 is a
   healthy replica again.

## Verification

```bash
# on the rebuilt host
mysql -u root -p'<mysql_root_password>' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source|Source_Host"
```
Expect `Replica_IO_Running: Yes`, `Replica_SQL_Running: Yes`,
`Seconds_Behind_Source: 0`, and `Source_Host` pointing at the current
primary.

## What could still go wrong (learning notes)

- **GTID divergence is a real dead end in this pass, not just a slow
  path.** Unlike MSSQL's automatic-seeding fallback (which always
  eventually works, it just takes longer for a bigger database), if
  `GTID_SUBTRACT` finds real divergence here, this guide's automation has
  no fallback. The correct manual recovery is the same shape as Oracle's
  documented gap: treat the host as a fresh replica build — stop it, wipe
  its datadir, and re-run a role-reversed version of
  `backup.yml`/`restore.yml` — but those task files hard-code
  vm5-as-source/vm6-as-target, so rebuilding a **role-reversed** host (vm5
  as replica, sourced from vm6) needs them generalized to look up the live
  primary dynamically instead of assuming the original static mapping.
  Flagging this as a known gap rather than writing untested cross-host
  reclone logic to paper over it — the same call the Oracle sync-rebuild
  guide made for the same underlying reason.
- **`live_primary_host` is an assumption, not a verified fact.** This task
  file picks "the other host in the pair" and trusts it's actually healthy
  and writable — it doesn't independently confirm that before reconnecting
  the target to it. Always run `POST /deploy/repl-status` first and read
  the actual result; if both hosts are somehow unhealthy (or, worse, both
  writable — true split-brain), reconnecting blindly could point the
  target at the wrong side.
- **A large replication backlog can outrun the 5-minute poll window.**
  `retries: 30, delay: 10` in the verify step is sized for this lab's small
  dataset — a bigger one, or a target that had accumulated a long backlog
  before it stopped, needs a longer window or a lag-aware retry rather than
  a fixed one.
