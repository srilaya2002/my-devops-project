# Oracle Data Guard Failover — Implementation Guide

> Extends the working Data Guard pair from
> [`oracle-fastapi-build-from-scratch.md`](oracle-fastapi-build-from-scratch.md)
> with a real disaster-recovery role-transition path: switch vm4 to primary
> (planned, no data loss) or fail over to it (forced, primary unreachable),
> and switch/fail back to vm3 later the same way. Grounded in the actual DG
> config already running (`MaxPerformance`/`ASYNC` transport, broker-managed,
> no Fast-Start Failover), not the generic
> [`oracle-dataguard-failover-design.md`](oracle-dataguard-failover-design.md)
> template it's named after — that doc sketches an "Observer"/automatic
> failover concept that isn't configured in this lab. This guide builds what
> actually applies here, the same relationship
> [`mssql-dr-failover-implementation.md`](mssql-dr-failover-implementation.md)
> has to its own generic design template.
>
> Same caveat as the rest of this doc series: **a plan to review, not a
> record of a working build** — nothing here has run against `devops_VM3`/
> `devops_VM4` yet.

## Design thinking

**Why switchover and failover are two separate playbooks, not one endpoint
with a `mode=` flag.** The MSSQL failover guide added a single
`failover.yml` that branches on `failover_mode: planned|forced`, because
both operations are the same DDL verb family (`ALTER AVAILABILITY GROUP ...
FAILOVER` / `... FORCE_FAILOVER_ALLOW_DATA_LOSS`). Oracle's broker already
treats these as two distinct, named commands — `SWITCHOVER TO <db>` and
`FAILOVER TO <db>` — with meaningfully different preconditions (switchover
validates readiness first and refuses if it isn't safe; failover doesn't,
because by definition you're calling it when the primary can't be asked).
Mirroring that with two task files/playbooks/endpoints is more honest than
folding them into one parameter the way the MSSQL guide could, because
there genuinely isn't a shared code path here worth deduplicating.

**Why `db_unique_name` is computed from a fixed per-host label, and why
that's not the same thing as "current role."** Every database in this pair
has a `db_unique_name` assigned once, permanently, at creation time —
`orcl_p` for the database that lives on vm3, `orcl_s` for the one on vm4 —
and that name **never changes**, even after a switchover or failover swaps
which one is actually `PRIMARY`. The inventory's `oracle_role=primary`/
`standby` hostvar (from the build guide) is really a label for *which
db_unique_name this host owns*, fixed at build time — not a live status
field. Both task files below compute `my_db_unique_name` from that label
via `primary_db_unique_name`/`standby_db_unique_name`, but every actual
role decision (is this host already `PRIMARY`? is it safe to switch?) comes
from a live query (`v$database.database_role`, `dgmgrl VALIDATE DATABASE`)
— never from the inventory label. Trusting the label for anything but "what
TNS alias does this host answer to" is exactly the kind of assumption that
breaks the second time you fail over.

**Why there's no TNS/SCAN listener step, unlike a production DG setup.**
Same gap the MSSQL guide flags for its AG listener: nothing in
`oracle-fastapi-build-from-scratch.md` created one, and there's no client
application in this lab to redirect transparently. Whoever calls this API
has to know which host is currently primary — `POST /deploy/validate`
(below) is that check. In production, Oracle clients typically get this for
free from `tnsnames.ora` entries that already list both addresses with
`FAILOVER=on`, or from FAN/TAF — neither is configured here.

**Why there's no new status playbook, unlike the MSSQL guide.** The MSSQL
failover guide had to add `ag_status.yml` from scratch. This one doesn't
need to: `oracle-fastapi-build-from-scratch.md`'s `validate.yml` (and
`POST /deploy/validate`) already does exactly this — role, open mode,
apply/transport lag, full broker configuration status — because it was
written anticipating this exact guide (see that doc's note under Part 4).
Precheck and postcheck below both just call the endpoint that already
exists.

**Why the endpoint takes `target` explicitly instead of inferring
direction.** Same reasoning as the MSSQL guide: in a real DR scenario the
current primary might be exactly the thing that's unreachable, so nothing
can safely poll it to figure out "the other one." The caller has to say
which host should end up primary; `switchover`/`failover` just make it so.

**Why failback needs no separate endpoint.** Both endpoints are
direction-agnostic — `target` just names whichever host should become
primary. Switching back from vm4 to vm3 later is the identical call with
`target=vm3`, once vm3 is healthy and reachable (see the sync-rebuild guide
for reinstating it after a *forced* failover specifically — a switchover
never needs that, since the broker reconfigures the old primary as a
standby automatically as part of the same operation).

## Prerequisites

The Data Guard pair from `oracle-fastapi-build-from-scratch.md` must
already be built and healthy — confirm with:
```bash
curl -X POST http://localhost:8001/api/v1/deploy/validate
tail -f logs/ansible.log
```
Expect the broker configuration to report `SUCCESS` on both `orcl_p` and
`orcl_s`, with `apply lag` at `0 seconds`. This guide only adds a
role-transition path on top of that.

## Stage-by-stage code

### 1. New role task: `ansible/roles/oracle_dg/tasks/switchover.yml`

Invoked with `--limit` against the single host being promoted — every task
here runs only on that host, same pattern the MSSQL guide's `failover.yml`
uses with `-l vm1`/`-l vm2`.

```yaml
---
# Planned Data Guard switchover -- zero data loss, requires the target to
# validate as ready. Invoked with --limit against the host being promoted.
# oracle_role (inventory hostvar) only identifies which db_unique_name this
# host owns -- it is NOT a live role, that always comes from v$database.

- name: Verify Oracle connectivity on the switchover target
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
    - switchover

- name: Compute this host's db_unique_name
  set_fact:
    my_db_unique_name: "{{ (oracle_role == 'primary') | ternary(primary_db_unique_name, standby_db_unique_name) }}"
  tags:
    - switchover

- name: Check current role of the switchover target
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT database_role FROM v\$database;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: current_role
  changed_when: false
  tags:
    - switchover

- name: Validate switchover readiness via the broker
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ my_db_unique_name | upper }} "VALIDATE DATABASE {{ my_db_unique_name }}"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: validate_check
  when: (current_role.stdout | trim) != 'PRIMARY'
  changed_when: false
  tags:
    - switchover

- name: Fail fast if the broker doesn't report this database ready for switchover
  fail:
    msg: >-
      Refusing a switchover: VALIDATE DATABASE did not report
      "Ready for Switchover: Yes" for {{ my_db_unique_name }}. See
      validate_check.stdout above for the broker's actual reason (usually
      apply lag or a broker-reported gap) before retrying.
  when: (current_role.stdout | trim) != 'PRIMARY' and 'Ready for Switchover:  Yes' not in validate_check.stdout
  tags:
    - switchover

- name: Perform the switchover
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ my_db_unique_name | upper }} <<EOF
    SWITCHOVER TO {{ my_db_unique_name }};
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  when: (current_role.stdout | trim) != 'PRIMARY'
  changed_when: true
  tags:
    - switchover

- name: Wait for target to report PRIMARY
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT database_role FROM v\$database;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: post_switchover_role
  retries: 12
  delay: 10
  until: (post_switchover_role.stdout | trim) == 'PRIMARY'
  changed_when: false
  tags:
    - switchover

- name: Display new role
  debug:
    msg: "{{ my_db_unique_name }} is now {{ post_switchover_role.stdout | trim }}"
  tags:
    - switchover
```

The `Ready for Switchover:  Yes` match string is copied from typical 19c
`dgmgrl` output, including its double space — real output formatting can
shift slightly by patch level, so treat this exact string as a starting
point to confirm (or adjust) against what your first real run actually
prints, the same "verify against real output" caveat that applies
everywhere else in this doc series.

Note what this file does **not** do to the old primary: nothing. Unlike a
task-file-level "old primary cleanup" step, `SWITCHOVER TO` is a single
broker operation that reconfigures **both** databases as part of the same
command — the old primary comes back as a healthy standby automatically,
no separate Ansible task required.

### 2. New role task: `ansible/roles/oracle_dg/tasks/failover.yml`

Invoked with `--limit` against the surviving standby being promoted. No
`VALIDATE DATABASE` precondition here — by definition, you're calling this
because the current primary can't be reached to ask.

```yaml
---
# Forced Data Guard failover -- for when the current primary is unreachable.
# May lose whatever redo hadn't shipped yet (MaxPerformance/ASYNC transport).
# Invoked with --limit against the surviving standby being promoted.

- name: Verify Oracle connectivity on the failover target
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
    - failover

- name: Compute this host's db_unique_name
  set_fact:
    my_db_unique_name: "{{ (oracle_role == 'primary') | ternary(primary_db_unique_name, standby_db_unique_name) }}"
  tags:
    - failover

- name: Check current role of the failover target
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT database_role FROM v\$database;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: current_role
  changed_when: false
  tags:
    - failover

- name: Warn that this is a data-loss-possible operation
  debug:
    msg: >-
      Proceeding with FAILOVER TO {{ my_db_unique_name }}. MaxPerformance
      (ASYNC) transport means any redo not yet shipped from the old primary
      is lost. This is expected -- that's what "forced" means here.
  when: (current_role.stdout | trim) != 'PRIMARY'
  tags:
    - failover

- name: Perform the failover
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ my_db_unique_name | upper }} <<EOF
    FAILOVER TO {{ my_db_unique_name }};
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  when: (current_role.stdout | trim) != 'PRIMARY'
  changed_when: true
  tags:
    - failover

- name: Wait for target to report PRIMARY
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT database_role FROM v\$database;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: post_failover_role
  retries: 12
  delay: 10
  until: (post_failover_role.stdout | trim) == 'PRIMARY'
  changed_when: false
  tags:
    - failover

- name: Display new role
  debug:
    msg: "{{ my_db_unique_name }} is now {{ post_failover_role.stdout | trim }}"
  tags:
    - failover
```

Also deliberately does nothing to the old primary — after a forced
failover, the old primary (once it's reachable again) shows up in
`dgmgrl SHOW CONFIGURATION` as `ERROR` and needs `REINSTATE DATABASE`
(or, if flashback logging wasn't enabled and reinstatement fails, a full
`standby.yml` re-duplication). That recovery is the sync-rebuild guide's
job, same as the MSSQL plan's split between failover and sync-rebuild.

### 3. New playbook: `ansible/playbooks/switchover.yml`

```yaml
---
- name: Switch the Data Guard pair over to a new primary (planned)
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run switchover tasks
      include_role:
        name: oracle_dg
        tasks_from: switchover.yml
      tags:
        - switchover
```

### 4. New playbook: `ansible/playbooks/failover.yml`

```yaml
---
- name: Fail the Data Guard pair over to a new primary (forced)
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run failover tasks
      include_role:
        name: oracle_dg
        tasks_from: failover.yml
      tags:
        - failover
```

`hosts: oracle_servers` plus `--limit <target>` from the deployer (below)
is what scopes either playbook to a single host — same pattern
`site.yml -l vm3` already uses in the build guide's `rewind` sequence.

### 5. `app/deployer.py` additions

Add these two methods to `AnsibleOracleDeployer` (no new status method
needed — `deploy_validate` already exists from the build guide and is
reused directly, both here and by the routes below):

```python
def deploy_switchover(self, task_id: str, target: str) -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "switchover.yml",
            limit=target,
            extra_vars=self._build_extra_vars(),
        ),
    )

def deploy_failover(self, task_id: str, target: str) -> None:
    self._run_task(
        task_id,
        lambda: self.ansible.run_playbook(
            "failover.yml",
            limit=target,
            extra_vars=self._build_extra_vars(),
        ),
    )
```

### 6. `app/routes/deploy.py` additions

```python
@router.post("/switchover")
async def deploy_switchover(background_tasks: BackgroundTasks, target: str):
    """Switch the Data Guard pair over so `target` ('vm3' or 'vm4') becomes primary.

    Planned, zero-data-loss role reversal -- the broker validates readiness
    and refuses if the target isn't caught up. The old primary is
    automatically reconfigured as a standby by the same operation.
    """
    if target not in ("vm3", "vm4"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm3' or 'vm4'")

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
    """Fail the Data Guard pair over so `target` ('vm3' or 'vm4') becomes primary.

    Use only when the current primary is unreachable -- may lose redo not
    yet shipped under MaxPerformance/ASYNC transport. The old primary is
    left untouched; reinstate it via the sync-rebuild guide once it's back.
    """
    if target not in ("vm3", "vm4"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm3' or 'vm4'")

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
            "estimated_duration_minutes": 3,
        }
    except Exception as e:
        logger.error(f"Error initiating failover: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate failover: {str(e)}")
```

`target` is a plain query parameter, same reasoning as the MSSQL guide —
it shows up as a fillable text field in Swagger's "Try it out" with no
schema to write. Status checks reuse the existing
`POST /api/v1/deploy/validate` — no new route needed there.

### 7. Docs to update

- [`oracle-fastapi-build-from-scratch.md`](oracle-fastapi-build-from-scratch.md) —
  no changes needed; unlike the teardown guide, nothing in that doc's
  current text claims failover doesn't exist yet.
- Once `python-fastapi-oracle-dg/CHANGELOG.md`/`RUNBOOK.md` exist (they
  don't yet, per the teardown guide's own note) — add the
  `switchover`/`failover` addition there, matching whatever style the
  MSSQL project's docs settle into.

## Run it — CLI

**Planned switchover** (vm3 primary → vm4 primary):
```bash
# 1. Precheck: confirm current roles and lag
curl -X POST http://localhost:8001/api/v1/deploy/validate
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0].results'

# 2. Switchover
curl -X POST "http://localhost:8001/api/v1/deploy/switchover?target=vm4"
tail -f logs/ansible.log
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'

# 3. Postcheck: confirm vm4 is now PRIMARY and vm3 rejoined as a healthy standby
curl -X POST http://localhost:8001/api/v1/deploy/validate
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0].results'
```

**Switch back** — literally the same endpoint, other direction, once vm3
reports `SUCCESS` in the postcheck above:
```bash
curl -X POST "http://localhost:8001/api/v1/deploy/switchover?target=vm3"
```

**Simulating a real DR drill** (forced failover, primary genuinely down):
```bash
# on vm3, simulate an outage:
sudo systemctl stop oracle-database 2>/dev/null || sudo -u oracle bash -c \
  "\$ORACLE_HOME/bin/sqlplus -s / as sysdba <<'EOF'
SHUTDOWN ABORT;
EOF"

# from the controller (VM1), force vm4 to primary:
curl -X POST "http://localhost:8001/api/v1/deploy/failover?target=vm4"
tail -f logs/ansible.log

# postcheck
curl -X POST http://localhost:8001/api/v1/deploy/validate
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0].results'

# bring vm3 back up and reinstate it as a standby -- see the sync-rebuild guide
```

## Run it — Swagger UI

Open `http://<vm1-ip>:8001/api/docs`.

1. **POST /api/v1/deploy/validate** → **Try it out** → **Execute** (no
   params). Then **GET /api/v1/deploy/history** → **Execute** — confirm
   current roles and lag before touching anything.
2. **POST /api/v1/deploy/switchover** → **Try it out** → fill `target` =
   `vm4` → **Execute**. Response shows `"status": "initiated"` with a
   `task_id` — the switchover itself runs in the background.
3. Poll **GET /api/v1/deploy/history** → **Execute** — `status` moves
   `"running"` → `"success"`/`"failed"`.
4. **POST /api/v1/deploy/validate** again → **Execute**, then
   **GET /api/v1/deploy/history** → **Execute** — confirm vm4 now shows
   `PRIMARY` and the broker reports `SUCCESS` on both sides.

## Verification

```bash
# from either host, as the oracle user
dgmgrl sys/'<oracle_pwd>'@ORCL_S "SHOW CONFIGURATION VERBOSE"
```
After a successful switchover to vm4, expect `orcl_s` reporting `PRIMARY`
and `orcl_p` reporting `PHYSICAL STANDBY`, both `SUCCESS`. After a **forced**
failover specifically, expect the old primary's entry to show `ERROR` until
it's reinstated — check the sync-rebuild guide before trusting its health.

## What this lab's DR setup doesn't cover

Worth knowing as you build this, not because it needs fixing right now:
- **No Fast-Start Failover.** No Observer process is configured, so
  nothing detects an outage and fails over automatically — this guide's
  endpoints are the entire mechanism, the direct Oracle equivalent of the
  MSSQL lab having no cluster manager. Production DG setups add FSFO with
  a third host running the Observer for exactly this reason.
- **No TNS/SCAN-level client redirection.** Callers must know which host
  is currently primary (`POST /deploy/validate`) — there's no single
  connect string that transparently follows the primary role.
- **No monitoring/alerting** triggers any of this automatically. You are
  the monitoring in this lab.
