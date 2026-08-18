# MySQL Replication Failover — Implementation Guide

> Extends the working GTID replication pair from
> [`mysql-fastapi-build-from-scratch.md`](mysql-fastapi-build-from-scratch.md)
> with a real disaster-recovery role-transition path: switch vm6 to primary
> (planned, zero data loss) or fail over to it (forced, primary
> unreachable), and switch/fail back to vm5 later the same way. Grounded in
> the actual replication config already running (async GTID, no semi-sync,
> no built-in promotion command), not the generic
> [`mysql-replication-failover-design.md`](mysql-replication-failover-design.md)
> template it's named after — that doc sketches a `POST /dr/failover`-style
> API shape without engaging with the fact that, unlike MSSQL's AG or
> Oracle's broker, **MySQL replication has no built-in failover command at
> all**. This guide builds the actual mechanics, the same relationship
> [`mssql-dr-failover-implementation.md`](mssql-dr-failover-implementation.md)
> and
> [`oracle-dataguard-failover-implementation.md`](oracle-dataguard-failover-implementation.md)
> have to their own generic templates.
>
> Same caveat as the rest of this series: **a plan to review, not a record
> of a working build** — nothing here has run against `devops_VM5`/
> `devops_VM6` yet.

## Design thinking

**Why this guide has to do more manual work than the MSSQL/Oracle failover
guides.** `ALTER AVAILABILITY GROUP ... FAILOVER` and `dgmgrl SWITCHOVER
TO` are single commands because SQL Server and Oracle both have a
broker/cluster layer that already knows about both replicas and coordinates
the role swap for you. Plain MySQL GTID replication has none of that — a
"replica" is just a server running `START REPLICA` against a source, and
"promoting" it is nothing more than flipping `read_only` off. Reversing the
old primary into a new replica isn't automatic either; this guide's
`switchover.yml` does that reconfiguration itself, in the same playbook, as
explicit steps — there's no broker to hand it to.

**A real gap in the build guide this guide surfaces and works around:
`my.cnf.j2` never sets `read_only` on the replica.** Go back and check —
it's rendered identically on both hosts, with no `read_only` line at all.
That means right now, nothing actually stops a client from writing directly
to vm6 even while it's replicating normally; MySQL's replica-safety net
was simply never turned on. This guide's `switchover.yml`/`failover.yml`
both explicitly set `read_only`/`super_read_only` as part of promotion and
demotion regardless, so failover itself works correctly either way — but
the underlying gap is worth fixing at the source. Recommended follow-up
(not done here, to keep this guide's diff scoped to failover): add
`SET PERSIST read_only = ON;` to `replication.yml`'s post-`START REPLICA`
steps in the build guide, so the protection survives a restart and isn't
only ever set during a role transition.

**Why "planned" locks the old primary and waits for exact catch-up, rather
than trusting a lag metric alone.** `Seconds_Behind_Source: 0` is the
standard replica-health signal, but it's a coarse, timestamp-derived value
— reading `0` doesn't by itself prove every byte the primary ever committed
has been applied. This guide's `switchover.yml` sequences around that
instead of trusting the number alone: it sets `read_only`/`super_read_only`
on the *old* primary **first**, which blocks any further writes there, and
only then waits for the target's lag to reach `0`. Once no new writes are
possible and the target reports fully caught up, there is nothing left
un-replicated — the ordering is what makes it safe, not the metric by
itself. (A stricter version would also diff `@@GLOBAL.gtid_executed`
between both sides directly; noted as a possible hardening, not implemented
here, to keep the check readable.)

**Why `switchover`/`failover` pass `target` as an extra-var and act on both
hosts, instead of using `--limit <target>` like the MSSQL/Oracle guides.**
Those guides could scope to a single host because either the broker
handled the other side automatically (Oracle) or nothing needed to happen
to the other side at all in the forced case (MSSQL). MySQL's `switchover`
genuinely needs **both** hosts touched in one coordinated play — lock the
old primary, wait, promote the new one, then reconfigure the old primary as
a replica of the new one. `--limit` would remove the other host from the
play entirely, which doesn't work here. `failover.yml` only ever needs the
target host, but it's written the same way for consistency between the two
endpoints rather than mixing invocation styles.

**Why there's a new `repl_status.yml`, unlike the Oracle guide (which
reused `validate.yml`).** The Oracle build guide added `validate.yml` up
front specifically anticipating a future failover guide. The MySQL build
guide didn't add an equivalent — there was no existing read-only status
check to reuse, so this guide adds one from scratch, the same way MSSQL's
failover guide had to add `ag_status.yml`.

**Why the endpoint takes `target` explicitly instead of inferring
direction.** Same reasoning as the other two guides: in a real DR scenario
the current primary might be exactly the thing that's unreachable, so
nothing can safely poll it to figure out "the other one." The caller says
which host should end up primary.

**Why failback needs no separate endpoint.** Both endpoints are
direction-agnostic — `target` just names whichever host should become
primary. Switching back from vm6 to vm5 later is the identical call with
`target=vm5`, once vm5 is healthy and reachable (see the sync-rebuild guide
for reconciling it after a *forced* failover specifically — a switchover
never needs that, since this playbook reconfigures the old primary as a
replica automatically as part of the same operation).

## Prerequisites

The replication pair from `mysql-fastapi-build-from-scratch.md` must
already be built and healthy. Confirm directly until this guide's
`POST /deploy/repl-status` exists:
```bash
# on vm6
mysql -u root -p'<mysql_root_password>' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source"
```
Expect `Replica_IO_Running: Yes`, `Replica_SQL_Running: Yes`,
`Seconds_Behind_Source: 0`.

## Stage-by-stage code

### 1. New role task: `ansible/roles/mysql_repl/tasks/repl_status.yml`

Read-only snapshot of both hosts' writability and replication state.
Changes nothing — safe to run at any time, including mid-incident.

```yaml
---
# Read-only replication status snapshot -- safe to run any time, changes nothing.

- name: Verify MySQL connectivity
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT 1;"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - repl_status

- name: Check whether this host is currently writable
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT @@read_only, @@super_read_only;"
  register: read_only_check
  changed_when: false
  tags:
    - repl_status

- name: Check replica status (empty output means this host isn't anyone's replica)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G"
  register: replica_status
  changed_when: false
  tags:
    - repl_status

- name: Display replication status
  debug:
    msg:
      - "read_only / super_read_only: {{ read_only_check.stdout }}"
      - "replica status: {{ replica_status.stdout_lines }}"
  tags:
    - repl_status
```

### 2. New role task: `ansible/roles/mysql_repl/tasks/switchover.yml`

Planned role transition. Runs against both hosts in one play — receives
`switchover_target` as an extra-var naming whichever host should end up
primary.

```yaml
---
# Planned MySQL replication switchover -- zero data loss. Runs against both
# hosts; switchover_target (extra-var) names the host being promoted.

- name: Verify MySQL connectivity
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT 1;"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  tags:
    - switchover

- name: Lock writes on the current primary (the host not becoming target)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    SET GLOBAL read_only = ON;
    SET GLOBAL super_read_only = ON;
    "
  when: inventory_hostname != switchover_target
  changed_when: true
  tags:
    - switchover

- name: Wait for the target to fully catch up before promoting
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G" | grep "Seconds_Behind_Source"
  register: catchup_check
  retries: 12
  delay: 5
  until: "'Seconds_Behind_Source: 0' in catchup_check.stdout"
  when: inventory_hostname == switchover_target
  changed_when: false
  tags:
    - switchover

- name: Stop replica threads on the target (about to be promoted)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "STOP REPLICA;"
  when: inventory_hostname == switchover_target
  changed_when: true
  tags:
    - switchover

- name: Promote the target to a writable primary
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    SET GLOBAL read_only = OFF;
    SET GLOBAL super_read_only = OFF;
    "
  when: inventory_hostname == switchover_target
  changed_when: true
  tags:
    - switchover

- name: Reconfigure the old primary as a replica of the newly promoted target
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    RESET REPLICA ALL;
    CHANGE REPLICATION SOURCE TO
      SOURCE_HOST='{{ hostvars[switchover_target].ansible_host }}',
      SOURCE_PORT={{ mysql_port }},
      SOURCE_USER='{{ mysql_repl_user }}',
      SOURCE_PASSWORD='{{ mysql_repl_password }}',
      SOURCE_AUTO_POSITION=1;
    START REPLICA;
    "
  when: inventory_hostname != switchover_target
  changed_when: true
  tags:
    - switchover

- name: Wait for the old primary's replica threads to report healthy
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running"
  register: reversed_replica_status
  retries: 12
  delay: 5
  until: "'Replica_IO_Running: Yes' in reversed_replica_status.stdout and 'Replica_SQL_Running: Yes' in reversed_replica_status.stdout"
  when: inventory_hostname != switchover_target
  changed_when: false
  tags:
    - switchover

- name: Display final roles
  debug:
    msg: "{{ inventory_hostname }} is now {{ (inventory_hostname == switchover_target) | ternary('PRIMARY (writable)', 'REPLICA of ' + switchover_target) }}"
  tags:
    - switchover
```

Task ordering **is** the safety mechanism here — Ansible completes a task
across every host in the play before starting the next one, so "lock old
primary" is guaranteed finished everywhere before "wait for target to catch
up" begins anywhere. No `delegate_to` gymnastics needed to make one host's
task depend on another host's completed state.

### 3. New role task: `ansible/roles/mysql_repl/tasks/failover.yml`

Forced role transition — only ever touches the surviving target, since the
old primary is presumed unreachable.

```yaml
---
# Forced MySQL replication failover -- for when the current primary is
# unreachable. May lose any transaction committed there but not yet
# shipped via binlog (plain async replication, no semi-sync configured).
# Only ever touches failover_target (extra-var); the old primary is
# presumed unreachable and is not contacted.

- name: Verify MySQL connectivity on the failover target
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT 1;"
  register: connectivity_check
  retries: 5
  delay: 5
  until: connectivity_check.rc == 0
  changed_when: false
  when: inventory_hostname == failover_target
  tags:
    - failover

- name: Warn that this is a data-loss-possible operation
  debug:
    msg: >-
      Proceeding to promote {{ failover_target }} without contacting the
      old primary. Any transaction committed there but not yet shipped via
      binlog is lost -- that's what "forced" means here.
  when: inventory_hostname == failover_target
  tags:
    - failover

- name: Stop replica threads on the failover target
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "STOP REPLICA;"
  when: inventory_hostname == failover_target
  changed_when: true
  failed_when: false
  tags:
    - failover

- name: Reset replication configuration (this host is no longer anyone's replica)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "RESET REPLICA ALL;"
  when: inventory_hostname == failover_target
  changed_when: true
  failed_when: false
  tags:
    - failover

- name: Promote the target to a writable primary
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    SET GLOBAL read_only = OFF;
    SET GLOBAL super_read_only = OFF;
    "
  when: inventory_hostname == failover_target
  changed_when: true
  tags:
    - failover

- name: Verify the target is now writable
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT @@read_only, @@super_read_only;"
  register: writable_check
  when: inventory_hostname == failover_target
  changed_when: false
  tags:
    - failover

- name: Display new role
  debug:
    msg: "{{ failover_target }} is now PRIMARY (writable). {{ writable_check.stdout }}"
  when: inventory_hostname == failover_target
  tags:
    - failover
```

Deliberately does nothing to the old primary — once it's reachable again,
it still has its old replication config (or none at all, if the outage was
severe) and may hold transactions that never shipped. Reconciling it is
the sync-rebuild guide's job, same split as the other two DR guides in
this series.

### 4. New playbook: `ansible/playbooks/repl_status.yml`

```yaml
---
- name: Snapshot MySQL replication status
  hosts: mysql_servers
  become: yes
  gather_facts: no

  tasks:
    - name: Run replication status tasks
      include_role:
        name: mysql_repl
        tasks_from: repl_status.yml
      tags:
        - repl_status
```

### 5. New playbook: `ansible/playbooks/switchover.yml`

```yaml
---
- name: Switch MySQL replication over to a new primary (planned)
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run switchover tasks
      include_role:
        name: mysql_repl
        tasks_from: switchover.yml
      tags:
        - switchover
```

### 6. New playbook: `ansible/playbooks/failover.yml`

```yaml
---
- name: Fail MySQL replication over to a new primary (forced)
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run failover tasks
      include_role:
        name: mysql_repl
        tasks_from: failover.yml
      tags:
        - failover
```

Both playbooks run against the whole `mysql_servers` group, no `--limit` —
`switchover_target`/`failover_target` (passed as extra-vars from the
deployer, below) are what the task files branch on internally.

### 7. `app/deployer.py` additions

```python
def deploy_repl_status(self, task_id: str) -> None:
    self._run_task(task_id, lambda: self.ansible.run_playbook("repl_status.yml", extra_vars=self._build_extra_vars()))

def deploy_switchover(self, task_id: str, target: str) -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "switchover.yml",
            extra_vars={**self._build_extra_vars(), "switchover_target": target},
        ),
    )

def deploy_failover(self, task_id: str, target: str) -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "failover.yml",
            extra_vars={**self._build_extra_vars(), "failover_target": target},
        ),
    )
```

Note there's no `limit=` argument here, unlike the MSSQL/Oracle deployers'
equivalent methods — see the design-thinking note above for why.

### 8. `app/routes/deploy.py` additions

```python
@router.post("/repl-status")
async def deploy_repl_status(background_tasks: BackgroundTasks):
    """Snapshot each host's read_only flag and replica status. Read-only, safe any time."""
    logger.info("Received deployment request - Replication status")
    try:
        task_id = deployer.start_task("repl-status")
        background_tasks.add_task(deployer.deploy_repl_status, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Replication status check started",
            "engine": "ansible",
            "playbook": "repl_status.yml",
            "estimated_duration_minutes": 1,
        }
    except Exception as e:
        logger.error(f"Error checking replication status: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to check replication status: {str(e)}")


@router.post("/switchover")
async def deploy_switchover(background_tasks: BackgroundTasks, target: str):
    """Switch replication over so `target` ('vm5' or 'vm6') becomes the writable primary.

    Planned -- locks the current primary, waits for target to fully catch
    up, promotes it, then reconfigures the old primary as target's replica.
    """
    if target not in ("vm5", "vm6"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm5' or 'vm6'")

    logger.info(f"Received deployment request - Switchover to {target}")
    try:
        task_id = deployer.start_task(f"switchover-{target}")
        background_tasks.add_task(deployer.deploy_switchover, task_id, target)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": f"Switchover to {target} started",
            "engine": "ansible",
            "playbook": "switchover.yml",
            "target": target,
            "estimated_duration_minutes": 3,
        }
    except Exception as e:
        logger.error(f"Error initiating switchover: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate switchover: {str(e)}")


@router.post("/failover")
async def deploy_failover(background_tasks: BackgroundTasks, target: str):
    """Fail replication over so `target` ('vm5' or 'vm6') becomes the writable primary.

    Use only when the current primary is unreachable -- may lose any
    transaction not yet shipped via binlog. The old primary is left
    untouched; reconcile it via the sync-rebuild guide once it's back.
    """
    if target not in ("vm5", "vm6"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm5' or 'vm6'")

    logger.info(f"Received deployment request - Failover to {target}")
    try:
        task_id = deployer.start_task(f"failover-{target}")
        background_tasks.add_task(deployer.deploy_failover, task_id, target)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": f"Failover to {target} started",
            "engine": "ansible",
            "playbook": "failover.yml",
            "target": target,
            "estimated_duration_minutes": 2,
        }
    except Exception as e:
        logger.error(f"Error initiating failover: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate failover: {str(e)}")
```

`target` is a plain query parameter, same reasoning as the other two
guides — a fillable text field in Swagger's "Try it out," no schema to
write.

### 9. Docs to update

- [`mysql-fastapi-build-from-scratch.md`](mysql-fastapi-build-from-scratch.md) —
  recommended follow-up (not done in this pass): add
  `SET PERSIST read_only = ON;` to `replication.yml`'s post-`START REPLICA`
  steps on the replica, closing the gap flagged in Design thinking above.
- Once `python-fastapi-mysql/CHANGELOG.md`/`RUNBOOK.md` exist (they don't
  yet, per the teardown guide's own note) — add the
  `repl-status`/`switchover`/`failover` addition there.

## Run it — CLI

**Planned switchover** (vm5 primary → vm6 primary):
```bash
# 1. Precheck: confirm current roles and lag
curl -X POST http://localhost:8002/api/v1/deploy/repl-status
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0].results'

# 2. Switchover
curl -X POST "http://localhost:8002/api/v1/deploy/switchover?target=vm6"
tail -f logs/ansible.log
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'

# 3. Postcheck: confirm vm6 is now writable and vm5 rejoined as a healthy replica
curl -X POST http://localhost:8002/api/v1/deploy/repl-status
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0].results'
```

**Switch back** — literally the same endpoint, other direction, once vm5
reports healthy replica status in the postcheck above:
```bash
curl -X POST "http://localhost:8002/api/v1/deploy/switchover?target=vm5"
```

**Simulating a real DR drill** (forced failover, primary genuinely down):
```bash
# on vm5, simulate an outage:
sudo systemctl stop mysqld

# from the controller (VM1), force vm6 to primary:
curl -X POST "http://localhost:8002/api/v1/deploy/failover?target=vm6"
tail -f logs/ansible.log

# postcheck
curl -X POST http://localhost:8002/api/v1/deploy/repl-status
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0].results'

# bring vm5 back up and reconcile it as a replica -- see the sync-rebuild guide
sudo systemctl start mysqld   # on vm5
```

## Run it — Swagger UI

Open `http://<vm1-ip>:8002/api/docs`.

1. **POST /api/v1/deploy/repl-status** → **Try it out** → **Execute** (no
   params). Then **GET /api/v1/deploy/history** → **Execute** — confirm
   current roles and lag before touching anything.
2. **POST /api/v1/deploy/switchover** → **Try it out** → fill `target` =
   `vm6` → **Execute**. Response shows `"status": "initiated"` with a
   `task_id` — the switchover itself runs in the background.
3. Poll **GET /api/v1/deploy/history** → **Execute** — `status` moves
   `"running"` → `"success"`/`"failed"`.
4. **POST /api/v1/deploy/repl-status** again → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** — confirm vm6 now shows
   `read_only: 0` and vm5 shows healthy replica status.

## Verification

```bash
# on the new primary
mysql -u root -p'<mysql_root_password>' -e "SELECT @@read_only, @@super_read_only;"

# on the new replica
mysql -u root -p'<mysql_root_password>' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source|Source_Host"
```
After a successful switchover to vm6, expect `read_only`/`super_read_only`
both `0` on vm6, and vm5's replica status showing `Replica_IO_Running: Yes`,
`Replica_SQL_Running: Yes`, `Source_Host` pointing at vm6. After a
**forced** failover specifically, don't trust vm5's state at all once it
comes back — check the sync-rebuild guide before assuming anything about it.

## What this lab's DR setup doesn't cover

Worth knowing as you build this, not because it needs fixing right now:
- **No semi-synchronous replication.** Plain async GTID replication means
  even a *planned* switchover's safety depends entirely on this guide's
  lock-then-wait ordering, not on any guarantee MySQL itself enforces — a
  semi-sync setup (`rpl_semi_sync_source_enabled`) would let the primary
  itself refuse to acknowledge a commit until at least one replica has it,
  closing the gap at the protocol level instead of the orchestration level.
  Not configured here.
- **No automatic failure detection.** Nothing watches vm5 and calls
  `/failover` for you — this guide's endpoints are the entire mechanism,
  same as the other two DR guides in this series. Production MySQL HA
  setups typically add Orchestrator, MHA, or a Group Replication/InnoDB
  Cluster topology for that; none of those are in this lab.
- **No proxy/connection redirection.** Callers must know which host is
  currently primary (`POST /deploy/repl-status`) — there's no ProxySQL or
  virtual IP in front of this pair that would let clients keep using one
  connection string across a role change.
- **No monitoring/alerting** triggers any of this automatically. You are
  the monitoring in this lab.
