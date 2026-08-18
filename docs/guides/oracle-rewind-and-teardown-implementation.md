# Oracle Data Guard Lab Rewind & Teardown — Implementation Plan

> This is the concrete, Oracle-specific implementation of the generic pattern
> sketched in [`oracle-rewind-and-teardown-design.md`](oracle-rewind-and-teardown-design.md).
> That doc is a template shared with the MSSQL/MySQL design docs; this one is
> traced from the actual task files planned in
> [`oracle-fastapi-build-from-scratch.md`](oracle-fastapi-build-from-scratch.md)
> and is meant to be followed step by step, by hand, to implement it — the
> same relationship
> [`mssql-rewind-and-teardown-implementation.md`](mssql-rewind-and-teardown-implementation.md)
> has to the MSSQL build guide.
>
> **Same caveat as the build guide it extends: this is a plan to review, not
> a record of a working build.** Nothing here has been run against
> `devops_VM3`/`devops_VM4` yet — treat the first real run as the actual
> test, especially the `deinstall` step (Part 3 below), which has a
> real reputation for being finicky about silent/non-interactive mode.

## Context

`python-fastapi-oracle-dg/` (per the build guide) has no reverse path at
all yet — `POST /deploy/site`, `/standby`, and `/dataguard` only build
forward. Once you've built a pair once and want to retry with a fix, or
just want the lab back to a clean state, the only option today is manual
SSH + `dbca -deleteDatabase` + `deinstall` by hand. This plan adds real
Ansible task files + FastAPI endpoints for that, in the same tree the build
guide creates: `python-fastapi-oracle-dg/ansible/`. There's no AWX/GitLab
equivalent tree for Oracle the way `ansible-mssql-deploy/` diverges from
`python-fastapi-mssql/ansible/` — this is the only Oracle automation that
exists or is planned, so there's no scope question to resolve here the way
the MSSQL plan had to call one out.

Decisions, matching the MSSQL plan's precedent:
- **Safety**: new endpoints fire immediately, same as `install`/`standby`/`dataguard` today — no confirmation gate.
- **`reset-baseline`**: wipes Oracle back to a bare VM only. It does not reinstall — call `POST /deploy/install` (or `/full-dg`) afterward.
- **`software/` is never touched.** Unlike the MSSQL build's `local_backup_dir`/`local_cert_relay_dir` (both fully regenerable from a live database), the Oracle install zip in `python-fastapi-oracle-dg/software/` on the controller (VM1) was manually downloaded from Oracle and can't be re-fetched with a `curl`. Neither `teardown.yml` nor `uninstall.yml` deletes anything under `software/` — only `local_cert_relay_dir` (the password-file relay, trivially regenerable) gets cleared, the same way the MSSQL plan clears its cert relay dir but not, say, the AdventureWorks source `.bak` on the controller (which doesn't exist as a controller-side file in that build in the first place — this is a genuinely new distinction Oracle's teardown has to make that MSSQL's didn't).

The end goal: get the code changes in place, then run a **reset-baseline → full-dg**
cycle as proof the build goes straight through on a genuinely bare VM pair,
the same proof point the MSSQL plan used.

## What state actually needs reversing

Traced from the task files planned in the build guide:

| Created by | State | Reversed by |
|---|---|---|
| `prep.yml` | oracle user/groups, `ORACLE_BASE`/`ORACLE_HOME`/inventory/data/FRA/stage directories, kernel params, resource limits, SELinux permissive, THP-disabled grub entry | `uninstall.yml` (new) — directories and the user/groups only; kernel params, limits, SELinux, and grub are deliberately left alone, see below |
| `stage_software.yml` + `install_software.yml` | staged zip + extracted software under `ORACLE_HOME`, central inventory registration, `.bash_profile` env block | `uninstall.yml` (new) |
| `listener.yml` | `listener.ora`/`tnsnames.ora`, running listener process | `uninstall.yml` (new) — `teardown.yml` leaves the listener running, same way MSSQL's `teardown.yml` leaves `mssql-server` running |
| `primary_db.yml` | primary database `orcl` (`db_unique_name=orcl_p`), force logging, standby redo logs, DG transport params, password file relayed to controller | `teardown.yml` (new) |
| `standby_prep.yml` + `duplicate_standby.yml` | standby instance + duplicated database `orcl` (`db_unique_name=orcl_s`), managed recovery running | `teardown.yml` (new) |
| `dataguard_broker.yml` | broker configuration `orcl_dg` (both databases registered) | `teardown.yml` (new) |
| `site.yml` post_tasks | `/tmp/oracle_deployment_<host>.txt` | both |

**Ordering note (why `teardown.yml` removes the broker configuration before
touching either database) — and how this differs from the MSSQL AG's
ordering:** the MSSQL plan drops the Availability Group independently on
*each* replica, because `CLUSTER_TYPE=NONE` leaves no shared state for one
side to clean up on the other's behalf. Oracle's broker configuration is the
opposite shape — `REMOVE CONFIGURATION` is a **single** `dgmgrl` command,
issued once against the primary, that updates broker metadata stored in
*both* databases' control files at once. It only works while both instances
are still reachable (primary `OPEN`, standby at least `MOUNTED`), so it has
to run **before** either database is shut down or deleted — the reverse of
the MSSQL case, where the AG drop has to happen on both nodes precisely
*because* there's no single command that reaches both.

**Why the standby is deleted differently from the primary:** `dbca
-deleteDatabase` assumes an `OPEN`, primary-role database — it doesn't
reliably handle a physical standby sitting in `MOUNTED` state with managed
recovery active. `teardown.yml` therefore uses `dbca -deleteDatabase` on
vm3 (primary, `OPEN`) but does the standby side by hand on vm4: cancel
managed recovery, `SHUTDOWN ABORT` (safe here — everything's about to be
deleted anyway), then remove the standby's datafiles/controlfile/spfile/
password file directly. This is also just what most real-world DG teardown
runbooks do, not an automation shortcut.

**Why `uninstall.yml` doesn't revert kernel params, resource limits,
SELinux, or the THP grub entry:** matching the MSSQL plan's precedent of
leaving the firewalld port-5022 rule alone (re-enabling it is a no-op,
leaving it open is harmless) — these host-level settings are harmless to
leave in place on an Oracle-less VM, and reverting the grub change
specifically would need another reboot to take effect for no real benefit.
If you want a genuinely pristine VM back, that's a manual step, not
something `reset-baseline` attempts.

## File-by-file changes

### 1. New: `python-fastapi-oracle-dg/ansible/roles/oracle_dg/tasks/teardown.yml`

Reverses `dataguard_broker.yml` + `duplicate_standby.yml` + `standby_prep.yml`
+ `primary_db.yml`. Leaves Oracle software and the listener running on both
hosts. Every step is check-then-act or `failed_when: false`, so it's safe to
call on a host that never had a database, or to re-run twice in a row.

```yaml
---
# Reverse dataguard_broker.yml + duplicate_standby.yml + standby_prep.yml +
# primary_db.yml. Leaves Oracle software and the listener installed and
# running; safe to re-run.

- name: Check whether a broker configuration exists
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} "SHOW CONFIGURATION"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: dg_config_check
  when: inventory_hostname == 'vm3'
  changed_when: false
  failed_when: false
  tags:
    - teardown

- name: Disable and remove the broker configuration (updates both databases' control files)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} <<EOF
    DISABLE CONFIGURATION;
    REMOVE CONFIGURATION;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  when: inventory_hostname == 'vm3' and 'ORA-16532' not in dg_config_check.stdout
  changed_when: true
  failed_when: false
  tags:
    - teardown

- name: Check for an active standby database on this host
  stat:
    path: "{{ oracle_home }}/dbs/spfile{{ oracle_sid }}.ora"
  register: standby_spfile_check
  when: inventory_hostname == 'vm4'
  tags:
    - teardown

- name: Cancel managed recovery on the standby (safety net)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm4' and standby_spfile_check.stat.exists
  changed_when: false
  failed_when: false
  tags:
    - teardown

- name: Shut down the standby instance
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SHUTDOWN ABORT;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm4' and standby_spfile_check.stat.exists
  changed_when: true
  failed_when: false
  tags:
    - teardown

- name: Remove the standby's database files
  file:
    path: "{{ item }}"
    state: absent
  loop:
    - "{{ db_data_dir }}/{{ db_name }}"
    - "{{ oracle_home }}/dbs/spfile{{ oracle_sid }}.ora"
    - "{{ oracle_home }}/dbs/init{{ oracle_sid }}.ora"
    - "{{ oracle_home }}/dbs/orapw{{ oracle_sid }}"
  when: inventory_hostname == 'vm4'
  tags:
    - teardown

- name: Check for the primary database on this host
  stat:
    path: "{{ oracle_home }}/dbs/spfile{{ oracle_sid }}.ora"
  register: primary_spfile_check
  when: inventory_hostname == 'vm3'
  tags:
    - teardown

- name: Delete the primary database via dbca
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dbca -silent -deleteDatabase -sourceDB {{ oracle_sid }}
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_BASE: "{{ oracle_base }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm3' and primary_spfile_check.stat.exists
  changed_when: true
  tags:
    - teardown

- name: Remove leftover primary password file (dbca does not always clean this up)
  file:
    path: "{{ oracle_home }}/dbs/orapw{{ oracle_sid }}"
    state: absent
  when: inventory_hostname == 'vm3'
  tags:
    - teardown

- name: Remove deployment summary file
  file:
    path: "/tmp/oracle_deployment_{{ inventory_hostname }}.txt"
    state: absent
  tags:
    - teardown

- name: Clear controller-side password-file relay directory (vm3 only, runs once)
  file:
    path: "{{ local_cert_relay_dir }}"
    state: absent
  delegate_to: localhost
  become: false
  when: inventory_hostname == "vm3"
  tags:
    - teardown
```

Note what's deliberately absent from this file: nothing under
`{{ local_software_dir }}` on the controller, and no `file: state=absent`
against `{{ oracle_home }}` itself — `teardown.yml` only ever removes
*database* state, never the software installation. That's `uninstall.yml`'s
job.

### 2. New: `python-fastapi-oracle-dg/ansible/roles/oracle_dg/tasks/uninstall.yml`

Deep wipe — Oracle software, the oracle OS user/groups, and every directory
`prep.yml` created. Returns each host to a bare VM.

```yaml
---
# Deep wipe: removes Oracle software and the oracle OS account entirely,
# returning the host to a bare VM. Run teardown.yml first if a database or
# broker configuration might still exist -- this file does not attempt any
# database-aware cleanup, it shuts down whatever's running and deletes
# everything.

- name: Stop the listener
  become: yes
  become_user: "{{ oracle_user }}"
  shell: "{{ oracle_home }}/bin/lsnrctl stop {{ listener_name }}"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  failed_when: false
  tags:
    - uninstall

- name: Shut down any running instance (safety net if teardown.yml wasn't run first)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SHUTDOWN ABORT;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  changed_when: false
  failed_when: false
  tags:
    - uninstall

- name: Run Oracle's deinstall tool (best effort -- see note below)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    yes | {{ oracle_home }}/deinstall/deinstall -silent
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_BASE: "{{ oracle_base }}"
  register: deinstall_result
  changed_when: true
  failed_when: false
  tags:
    - uninstall

- name: Display deinstall result
  debug:
    msg: "{{ inventory_hostname }} deinstall rc={{ deinstall_result.rc }} -- forcing directory removal regardless, see next tasks"
  tags:
    - uninstall

- name: Force-remove Oracle directories regardless of deinstall's outcome
  file:
    path: "{{ item }}"
    state: absent
  loop:
    - "{{ oracle_home }}"
    - "{{ oracle_base }}"
    - "{{ oracle_inventory_dir }}"
    - "{{ oracle_stage_dir }}"
  tags:
    - uninstall

- name: Remove /etc/oratab entries for this SID (left behind by root.sh/dbca)
  lineinfile:
    path: /etc/oratab
    regexp: "^{{ oracle_sid }}:"
    state: absent
  failed_when: false
  tags:
    - uninstall

- name: Remove the oracle OS user (and its home directory)
  user:
    name: "{{ oracle_user }}"
    state: absent
    remove: yes
  failed_when: false
  tags:
    - uninstall

- name: Remove the oinstall/dba/oper groups
  group:
    name: "{{ item }}"
    state: absent
  loop: "{{ [oracle_group] + oracle_extra_groups }}"
  failed_when: false
  tags:
    - uninstall

- name: Remove deployment summary file
  file:
    path: "/tmp/oracle_deployment_{{ inventory_hostname }}.txt"
    state: absent
  tags:
    - uninstall

- name: Clear controller-side password-file relay directory (vm3 only, runs once)
  file:
    path: "{{ local_cert_relay_dir }}"
    state: absent
  delegate_to: localhost
  become: false
  when: inventory_hostname == "vm3"
  tags:
    - uninstall
```

The `deinstall` step is marked best-effort on purpose (`failed_when: false`,
followed unconditionally by a forced `file: state=absent` on the same
directories) — Oracle's deinstall tool is notoriously inconsistent about
exit codes and prompts under true non-interactive automation, and `yes |`
piping `y` at anything it asks is a blunt instrument. Treat the `debug`
message's `rc` as informational; the directory removal that follows is what
actually guarantees a clean host either way.

### 3. New: `python-fastapi-oracle-dg/ansible/playbooks/teardown.yml`

```yaml
---
- name: Tear down the Data Guard broker configuration and both databases
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  pre_tasks:
    - name: Check Oracle connectivity before teardown
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
      failed_when: false
      tags:
        - teardown

  tasks:
    - name: Run teardown tasks
      include_role:
        name: oracle_dg
        tasks_from: teardown.yml
      when: connectivity_check.rc == 0
      tags:
        - teardown
```

Same shape as the MSSQL plan's `teardown.yml` — the connectivity gate makes
it safe to call `/deploy/teardown` against a host where Oracle isn't even
running (e.g. already torn down): it skips instead of erroring. `SELECT 1
FROM dual` works whether the local instance is `OPEN` (vm3) or `MOUNTED`
(vm4, standby) — a mounted standby still accepts a local `sqlplus / as
sysdba` bequeath connection even though it can't serve normal queries.

### 4. New: `python-fastapi-oracle-dg/ansible/playbooks/uninstall.yml`

```yaml
---
- name: Remove Oracle entirely and return both hosts to a bare VM
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run uninstall tasks
      include_role:
        name: oracle_dg
        tasks_from: uninstall.yml
      tags:
        - uninstall
```

No connectivity gate, matching the MSSQL plan's `uninstall.yml` — it's
meant to work even on a host in a half-broken state where a connectivity
check itself might fail.

### 5. Edit: `python-fastapi-oracle-dg/app/deployer.py`

Add these methods to `AnsibleOracleDeployer` (near `deploy_full_dg`), reusing
the existing `SequenceStepError` pattern from `_run_full_dg_sequence`:

```python
def deploy_teardown(self, task_id: str) -> None:
    self._run_task(task_id, lambda: self.ansible.run_playbook("teardown.yml", extra_vars=self._build_extra_vars()))

def deploy_reset_baseline(self, task_id: str) -> None:
    self._run_task(task_id, lambda: self.ansible.run_playbook("uninstall.yml", extra_vars=self._build_extra_vars()))

def deploy_rewind(self, task_id: str) -> None:
    self._run_task(task_id, self._run_rewind_sequence)

def _run_rewind_sequence(self) -> Dict[str, object]:
    results: Dict[str, object] = {}
    results["teardown"] = self.ansible.run_playbook("teardown.yml", extra_vars=self._build_extra_vars())
    if not results["teardown"]["success"]:
        raise SequenceStepError("teardown step failed; see results.teardown for details", results)

    results["reinstall_primary"] = self.ansible.run_playbook(
        "site.yml",
        limit="vm3",
        extra_vars=self._build_extra_vars(),
    )
    if not results["reinstall_primary"]["success"]:
        raise SequenceStepError("reinstall_primary step failed; see results.reinstall_primary for details", results)

    return results

def get_rewind_plan(self) -> Dict[str, object]:
    """Static description of each destructive playbook -- not a live dry-run,
    since these are shell/sqlplus/dgmgrl tasks that Ansible --check can't safely simulate."""
    return {
        "note": "Describes what each playbook does; not a live check-mode run.",
        "teardown": {
            "playbook": "teardown.yml",
            "endpoint": "POST /api/v1/deploy/teardown",
            "leaves_oracle_installed": True,
            "steps": [
                "Remove the Data Guard broker configuration (once, from vm3, before touching either database)",
                "Cancel managed recovery and shut down the standby on vm4, then remove its database files directly",
                "Delete the primary database on vm3 via dbca -deleteDatabase",
                "Clear the controller-side password-file relay directory",
            ],
        },
        "rewind": {
            "playbooks": ["teardown.yml", "site.yml (limit vm3)"],
            "endpoint": "POST /api/v1/deploy/rewind",
            "leaves_oracle_installed": True,
            "steps": [
                "Everything in teardown, then",
                "Re-run site.yml against vm3 only: reconfirm prep/software/listener and create a fresh primary database",
                "Leaves vm3 with a clean primary and no broker configuration -- vm4 still has Oracle installed but no database -- ready to retry standby/dataguard",
            ],
        },
        "reset-baseline": {
            "playbook": "uninstall.yml",
            "endpoint": "POST /api/v1/deploy/reset-baseline",
            "leaves_oracle_installed": False,
            "steps": [
                "Stop the listener, shut down any running instance",
                "Run Oracle's deinstall tool (best effort), then force-remove ORACLE_HOME/ORACLE_BASE/inventory/stage directories regardless",
                "Remove the oracle OS user and oinstall/dba/oper groups",
                "Clear the controller-side password-file relay directory (software/ on the controller is never touched)",
                "Both hosts return to a bare VM -- call POST /api/v1/deploy/install to rebuild",
            ],
        },
    }
```

### 6. Edit: `python-fastapi-oracle-dg/app/routes/deploy.py`

Add four routes (same try/except/logging shape as the existing ones):

```python
@router.post("/teardown")
async def deploy_teardown(background_tasks: BackgroundTasks):
    """Tear down the Data Guard broker configuration and both databases.

    Leaves Oracle software and the listener installed and running on both
    hosts. Safe to re-run.
    """
    logger.info("Received deployment request - Teardown")
    try:
        task_id = deployer.start_task("teardown")
        background_tasks.add_task(deployer.deploy_teardown, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Teardown started",
            "engine": "ansible",
            "playbook": "teardown.yml",
            "estimated_duration_minutes": 10,
        }
    except Exception as e:
        logger.error(f"Error initiating teardown: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate teardown: {str(e)}")


@router.post("/rewind")
async def deploy_rewind(background_tasks: BackgroundTasks):
    """Tear down the broker/both databases, then rebuild a fresh primary on vm3.

    Leaves vm4 with Oracle installed but no database -- ready to retry
    POST /deploy/standby then /deploy/dataguard from a clean primary.
    """
    logger.info("Received deployment request - Rewind")
    try:
        task_id = deployer.start_task("rewind")
        background_tasks.add_task(deployer.deploy_rewind, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Rewind started",
            "engine": "ansible",
            "playbooks": ["teardown.yml", "site.yml"],
            "estimated_duration_minutes": 45,
        }
    except Exception as e:
        logger.error(f"Error initiating rewind: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate rewind: {str(e)}")


@router.post("/reset-baseline")
async def deploy_reset_baseline(background_tasks: BackgroundTasks):
    """Uninstall Oracle entirely and return both hosts to a bare VM.

    Does not reinstall -- call POST /api/v1/deploy/install afterward to rebuild.
    """
    logger.info("Received deployment request - Reset baseline")
    try:
        task_id = deployer.start_task("reset-baseline")
        background_tasks.add_task(deployer.deploy_reset_baseline, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Reset to bare-VM baseline started",
            "engine": "ansible",
            "playbook": "uninstall.yml",
            "estimated_duration_minutes": 15,
        }
    except Exception as e:
        logger.error(f"Error initiating reset-baseline: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate reset-baseline: {str(e)}")


@router.get("/rewind-plan")
async def get_rewind_plan():
    """Return the step-by-step plan for teardown/rewind/reset-baseline without executing anything."""
    try:
        return deployer.get_rewind_plan()
    except Exception as e:
        logger.error(f"Error retrieving rewind plan: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve rewind plan: {str(e)}")
```

### 7. Edit: `python-fastapi-oracle-dg/tests/test_api.py`

Same fixture shape as the MSSQL service's test file (`TestClient(app)` via a
`client` fixture) — add a GET test (no side effects, safe in CI) and a
mocked-sequencing test. **Do not** add a test that calls
`client.post("/api/v1/deploy/teardown")` (or `/rewind`, `/reset-baseline`)
directly, for the same reason the MSSQL suite never does: Starlette's
`TestClient` runs `BackgroundTasks` synchronously inside the request, which
would genuinely shell out to `ansible-playbook` against the real inventory
during `pytest`.

```python
def test_rewind_plan_endpoint(client):
    """GET-only, no side effects, safe to call in CI."""
    response = client.get("/api/v1/deploy/rewind-plan")
    assert response.status_code == 200
    data = response.json()
    assert "teardown" in data and "rewind" in data and "reset-baseline" in data


def test_deploy_rewind_sequence_stops_on_teardown_failure(monkeypatch):
    """Rewind must not attempt site.yml if teardown itself fails."""
    from app.deployer import AnsibleOracleDeployer, SequenceStepError

    deployer = AnsibleOracleDeployer()
    calls = []

    def fake_run_playbook(playbook_name, tags=None, limit=None, extra_vars=None, skip_tags=None):
        calls.append(playbook_name)
        return {"success": False, "playbook": playbook_name, "stdout": "", "stderr": "boom"}

    monkeypatch.setattr(deployer.ansible, "run_playbook", fake_run_playbook)

    try:
        deployer._run_rewind_sequence()
        assert False, "expected SequenceStepError"
    except SequenceStepError:
        pass

    assert calls == ["teardown.yml"]
```

### 8. Docs to update

- [`oracle-fastapi-build-from-scratch.md`](oracle-fastapi-build-from-scratch.md)
  **Part 9 — Rerun it**: replace the paragraph starting "There's no
  teardown/rewind/reset-baseline playbook for this build yet" with a pointer
  to this document, the same way the MSSQL build guide's Part 9 now points
  at its teardown/rewind doc instead of describing manual `deinstall` steps.
- Once you have a `python-fastapi-oracle-dg/CHANGELOG.md`/`RUNBOOK.md` (this
  build doesn't have either yet, unlike the MSSQL service) — add the
  teardown/rewind/reset-baseline addition there too, matching the MSSQL
  project's documentation shape once it exists.

## Commands to run it yourself

All from `python-fastapi-oracle-dg/` unless noted, on VM1 (the controller).

**1. Syntax-check the two new playbooks before touching real state:**
```bash
cd python-fastapi-oracle-dg
ansible-playbook -i ansible/inventory/hosts.ini ansible/playbooks/teardown.yml --syntax-check
ansible-playbook -i ansible/inventory/hosts.ini ansible/playbooks/uninstall.yml --syntax-check
```

**2. Run the new pytest cases (pure/mocked, no real ansible-playbook calls):**
```bash
source .venv/bin/activate
pytest tests/test_api.py -v -k "rewind"
```

**3. Restart the API so the new routes register** (uvicorn `--reload` should pick up the file changes automatically, but if it doesn't):
```bash
# Ctrl+C the running uvicorn, then:
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

**4. Exercise each endpoint individually and watch the log:**
```bash
curl http://localhost:8001/api/v1/deploy/rewind-plan | jq
curl -X POST http://localhost:8001/api/v1/deploy/teardown
tail -f logs/ansible.log        # watch it run
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'
```

**5. The real proof — reset to bare VM, then rebuild straight through with no manual fixes:**
```bash
# Wipe both VMs back to bare state
curl -X POST http://localhost:8001/api/v1/deploy/reset-baseline
tail -f logs/ansible.log     # wait for it to finish; check history for status=success
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'

# Rebuild everything in one call: provision both, create primary, duplicate standby, configure DG
curl -X POST http://localhost:8001/api/v1/deploy/full-dg
tail -f logs/ansible.log
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'
```

**6. Independently verify Data Guard is healthy** (don't just trust the playbook's own output):
```bash
# on vm3, as the oracle user
dgmgrl sys/'<oracle_pwd>'@ORCL_P "SHOW CONFIGURATION VERBOSE"
```
Expect `PRIMARY` / `orcl_p` and `PHYSICAL STANDBY` / `orcl_s` both reporting
`SUCCESS`, with `apply lag` at `0 seconds`.

**7. Practice a mid-cycle rewind** (tear down the DG pair only, keep Oracle installed on both hosts, retry from a clean primary):
```bash
curl -X POST http://localhost:8001/api/v1/deploy/rewind
tail -f logs/ansible.log
curl -X POST http://localhost:8001/api/v1/deploy/standby
curl -X POST http://localhost:8001/api/v1/deploy/dataguard
```

## Verification checklist

- [ ] `--syntax-check` passes on both new playbooks
- [ ] New pytest cases pass, no real `ansible-playbook` subprocess triggered by them
- [ ] `GET /rewind-plan` returns the three sections with no side effects
- [ ] `POST /teardown` on a host with an existing DG pair succeeds; re-running it immediately after (nothing left to tear down) also succeeds (idempotency)
- [ ] `POST /reset-baseline` leaves `id oracle` failing (no such user) and `ORACLE_HOME` gone on both VMs
- [ ] `POST /full-dg` after `reset-baseline` reaches a broker configuration reporting `SUCCESS` with `0 seconds` apply lag, with no manual SSH intervention
- [ ] `POST /rewind` after a working DG pair leaves vm3 with a clean primary database and no broker configuration, confirmed via `dgmgrl ... "SHOW CONFIGURATION"` returning `ORA-16532: no Data Guard broker configuration is available` (or equivalent) and vm4 still having Oracle software installed but no database at `{{ oracle_home }}/dbs/spfile{{ oracle_sid }}.ora`
