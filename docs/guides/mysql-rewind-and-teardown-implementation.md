# MySQL Replication Lab Rewind & Teardown — Implementation Plan

> This is the concrete, MySQL-specific implementation of the generic pattern
> sketched in [`mysql-rewind-and-teardown-design.md`](mysql-rewind-and-teardown-design.md).
> That doc is a template shared with the MSSQL/Oracle design docs; this one
> is traced from the actual task files in
> [`mysql-fastapi-build-from-scratch.md`](mysql-fastapi-build-from-scratch.md)
> and is meant to be followed step by step, by hand, to implement it — the
> same relationship
> [`oracle-rewind-and-teardown-implementation.md`](oracle-rewind-and-teardown-implementation.md)
> has to the Oracle build guide.
>
> Same caveat as the rest of this series: **a plan to review, not a record
> of a working build** — nothing here has run against `devops_VM5`/
> `devops_VM6` yet.

## Context

`python-fastapi-mysql/` (per the build guide) has no reverse path at all
yet — `POST /deploy/install`, `/backup`, and `/replication` only build
forward. This plan adds a real teardown/rewind/reset-baseline path, in the
same tree the build guide creates: `python-fastapi-mysql/ansible/`. There's
no AWX/GitLab-driven parallel tree for MySQL the way `ansible-mssql-deploy/`
diverges from `python-fastapi-mssql/ansible/` — same as the Oracle plan,
there's no scope question to resolve here.

Decisions, matching the MSSQL/Oracle plans' precedent:
- **Safety**: new endpoints fire immediately, no confirmation gate.
- **`reset-baseline`**: wipes MySQL back to a bare VM only. It does not
  reinstall — call `POST /deploy/install` (or `/full-repl`) afterward.
- **`local_backup_dir` is cleared, unlike Oracle's `software/`.** MySQL's
  XtraBackup snapshot is entirely regenerable from a live primary — there's
  no manually-downloaded install media in this build the way there is for
  Oracle, so nothing here needs the same "never touch this" carve-out that
  guide had to add.

The end goal: get the code changes in place, then run a **reset-baseline →
full-repl** cycle as proof the build goes straight through on a genuinely
bare VM pair, the same proof point the other two plans used.

## What state actually needs reversing

Traced from the task files in the build guide:

| Created by | State | Reversed by |
|---|---|---|
| `install.yml` | `mysql-community-server`/`-client` packages, `percona-xtrabackup` package, 2 yum repo definitions (MySQL + Percona), `/root/.mysql_bootstrapped` marker | `uninstall.yml` (new) |
| `configure.yml` | `log_dir`, `backup_dir` (owned `mysql:mysql`), `/etc/my.cnf.d/lab-replication.cnf` | `uninstall.yml` (new) |
| `replication_user.yml` | `repl`@`%` user + `REPLICATION SLAVE` grant (vm5) | `teardown.yml` (new) |
| `backup.yml` | XtraBackup snapshot files in `backup_dir` (vm5) | `teardown.yml` (new) |
| `restore.yml` | vm6's restored `data_dir`, `gtid_purged` set, `local_backup_dir` on the controller, relayed backup files in vm6's `backup_dir` | `teardown.yml` (new) |
| `replication.yml` | Replica threads running, `CHANGE REPLICATION SOURCE TO` config, GTID replication metadata | `teardown.yml` (new) |
| `site.yml` post_tasks | `/tmp/mysql_deployment_<host>.txt` | both |

**Ordering note — why this teardown is asymmetric between vm5 and vm6,
unlike MSSQL's AG or Oracle's broker configuration.** MSSQL's Availability
Group needs an independent `DROP AVAILABILITY GROUP` on *each* replica
(`CLUSTER_TYPE=NONE` has no shared state). Oracle's broker configuration is
the opposite — one `REMOVE CONFIGURATION` command, issued once, updates
both databases' control files at once. MySQL replication is neither: almost
**all** of the replication-specific state lives on the replica (vm6) —
`CHANGE REPLICATION SOURCE TO`, the running replica threads, and the GTID
position it tracks. vm5's *only* replication-specific state is the `repl`
user account; nothing on vm5 needs to be told the replica is going away.
`teardown.yml` reflects that directly: it does real work on vm6 (stop
threads, reset replication, wipe and reinitialize the datadir) and almost
nothing on vm5 (drop one user, clear backup artifacts).

**Why vm6 gets a full datadir wipe + reinitialize, not just
`RESET REPLICA ALL`.** Unlike MSSQL's `AdventureWorks` or Oracle's `orcl` —
one named user database that can be dropped on its own, leaving the rest of
the instance untouched — vm6's entire datadir came from the XtraBackup
restore in the first place. There's no pre-existing, independent data on
vm6 to preserve; everything in `{{ data_dir }}` is a byte-for-byte copy of
vm5 as of the backup. `RESET REPLICA ALL` alone would leave that copied
data sitting there, undifferentiated from a real independent database — a
full wipe and `mysqld --initialize-insecure` is what actually returns vm6
to "MySQL installed, no meaningful data," the equivalent end state to
MSSQL/Oracle's teardown.

**Why vm6's root password needs re-securing after the reinitialize.**
`mysqld --initialize-insecure` creates a brand-new system schema with a
blank root password and `validate_password` back at its packaged default
(enabled) — both of `install.yml`'s one-time bootstrap steps (`ALTER USER`,
`UNINSTALL COMPONENT`) only applied to the *previous* system schema, which
just got wiped along with everything else. `teardown.yml` repeats both
steps against the fresh instance so vm6 doesn't sit there with a blank
root password, even briefly, even in a lab.

## File-by-file changes

### 1. New: `python-fastapi-mysql/ansible/roles/mysql_repl/tasks/teardown.yml`

Reverses `replication.yml` + `backup.yml` + `restore.yml` +
`replication_user.yml`. Leaves MySQL installed and running on both hosts.

```yaml
---
# Reverse replication.yml + backup.yml + restore.yml + replication_user.yml.
# Leaves MySQL installed and configured on both hosts; safe to re-run.

- name: Stop replica threads on vm6
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "STOP REPLICA;"
  when: inventory_hostname == 'vm6'
  changed_when: true
  failed_when: false
  tags:
    - teardown

- name: Reset replication configuration on vm6 (clears CHANGE REPLICATION SOURCE + GTID metadata)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "RESET REPLICA ALL;"
  when: inventory_hostname == 'vm6'
  changed_when: true
  failed_when: false
  tags:
    - teardown

- name: Stop mysqld on vm6 before wiping its replicated data
  systemd:
    name: mysqld
    state: stopped
  when: inventory_hostname == 'vm6'
  tags:
    - teardown

- name: Wipe vm6's datadir (everything there came from the replication relationship)
  file:
    path: "{{ data_dir }}"
    state: "{{ item }}"
    owner: mysql
    group: mysql
    mode: '0750'
  loop:
    - absent
    - directory
  when: inventory_hostname == 'vm6'
  tags:
    - teardown

- name: Reinitialize vm6 as a bare MySQL instance
  shell: |
    mysqld --initialize-insecure --user=mysql --datadir={{ data_dir }}
  when: inventory_hostname == 'vm6'
  changed_when: true
  tags:
    - teardown

- name: Start mysqld on vm6 (bare instance, no replication)
  systemd:
    name: mysqld
    state: started
  when: inventory_hostname == 'vm6'
  tags:
    - teardown

- name: Wait for mysqld to come up on vm6
  wait_for:
    host: "{{ ansible_host }}"
    port: "{{ mysql_port }}"
    delay: 5
    timeout: 60
  when: inventory_hostname == 'vm6'
  tags:
    - teardown

- name: Secure the reinitialized instance's root password on vm6
  shell: |
    mysql -u root -e "
    ALTER USER 'root'@'localhost' IDENTIFIED BY '{{ mysql_root_password }}';
    UNINSTALL COMPONENT 'file://component_validate_password';
    "
  when: inventory_hostname == 'vm6'
  changed_when: true
  failed_when: false
  tags:
    - teardown

- name: Drop the replication user on vm5
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "DROP USER IF EXISTS '{{ mysql_repl_user }}'@'%';"
  when: inventory_hostname == 'vm5'
  changed_when: true
  tags:
    - teardown

- name: Remove backup artifacts
  file:
    path: "{{ backup_dir }}"
    state: absent
  tags:
    - teardown

- name: Remove deployment summary file
  file:
    path: "/tmp/mysql_deployment_{{ inventory_hostname }}.txt"
    state: absent
  tags:
    - teardown

- name: Clear controller-side backup relay directory (vm5 only, runs once)
  file:
    path: "{{ local_backup_dir }}"
    state: absent
  delegate_to: localhost
  become: false
  when: inventory_hostname == "vm5"
  tags:
    - teardown
```

`Remove backup artifacts` runs unconditionally on both hosts — `backup_dir`
holds the original XtraBackup snapshot on vm5 and the relayed copy on vm6,
both equally regenerable, so there's no need for the two separate
per-host tasks the earlier state table might suggest.

### 2. New: `python-fastapi-mysql/ansible/roles/mysql_repl/tasks/uninstall.yml`

Deep wipe — packages, repos, all three managed directories, and the
lab-specific config file. Returns each host to a bare VM.

```yaml
---
# Deep wipe: removes MySQL and Percona XtraBackup entirely, returning the
# host to a bare VM. Run teardown.yml first if replication might still be
# active -- this file does not attempt any replication-aware cleanup, it
# just stops the service and deletes everything.

- name: Stop mysqld
  systemd:
    name: mysqld
    state: stopped
  failed_when: false
  tags:
    - uninstall

- name: Remove MySQL Community Server and client packages
  yum:
    name:
      - "mysql-community-server-{{ mysql_version }}"
      - "mysql-community-client-{{ mysql_version }}"
    state: absent
  tags:
    - uninstall

- name: Remove Percona XtraBackup package
  yum:
    name: "{{ percona_xtrabackup_package }}"
    state: absent
  tags:
    - uninstall

- name: Remove the MySQL Yum repository package
  yum:
    name: mysql80-community-release
    state: absent
  failed_when: false
  tags:
    - uninstall

- name: Remove the Percona repository package
  yum:
    name: percona-release
    state: absent
  failed_when: false
  tags:
    - uninstall

- name: Remove the data directory
  file:
    path: "{{ data_dir }}"
    state: absent
  tags:
    - uninstall

- name: Remove the log directory
  file:
    path: "{{ log_dir }}"
    state: absent
  tags:
    - uninstall

- name: Remove the backup directory
  file:
    path: "{{ backup_dir }}"
    state: absent
  tags:
    - uninstall

- name: Remove the lab replication config file
  file:
    path: /etc/my.cnf.d/lab-replication.cnf
    state: absent
  tags:
    - uninstall

- name: Remove the root password bootstrap marker
  file:
    path: /root/.mysql_bootstrapped
    state: absent
  tags:
    - uninstall

- name: Reset any failed systemd unit state
  command: systemctl reset-failed mysqld
  failed_when: false
  changed_when: false
  tags:
    - uninstall

- name: Remove deployment summary file
  file:
    path: "/tmp/mysql_deployment_{{ inventory_hostname }}.txt"
    state: absent
  tags:
    - uninstall

- name: Clear controller-side backup relay directory (vm5 only, runs once)
  file:
    path: "{{ local_backup_dir }}"
    state: absent
  delegate_to: localhost
  become: false
  when: inventory_hostname == "vm5"
  tags:
    - uninstall
```

Removing the two release packages (`mysql80-community-release`,
`percona-release`) also removes the Yum repo files they own — no separate
`yum_repository: state=absent` step needed the way the MSSQL/Oracle plans
used for repos that were added a different way. Both are `failed_when:
false` since a host that never got past `install.yml`'s repo-setup steps
wouldn't have them installed at all.

### 3. New: `python-fastapi-mysql/ansible/playbooks/teardown.yml`

```yaml
---
- name: Tear down MySQL replication and backup artifacts
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  pre_tasks:
    - name: Check MySQL connectivity before teardown
      shell: |
        mysql -u root -p'{{ mysql_root_password }}' -e "SELECT 1;"
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
        name: mysql_repl
        tasks_from: teardown.yml
      when: connectivity_check.rc == 0
      tags:
        - teardown
```

Same shape as the other two plans' `teardown.yml` — the connectivity gate
makes it safe to call `/deploy/teardown` against a host where MySQL isn't
even running: it skips instead of erroring.

### 4. New: `python-fastapi-mysql/ansible/playbooks/uninstall.yml`

```yaml
---
- name: Remove MySQL entirely and return both hosts to a bare VM
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Run uninstall tasks
      include_role:
        name: mysql_repl
        tasks_from: uninstall.yml
      tags:
        - uninstall
```

### 5. Edit: `python-fastapi-mysql/app/deployer.py`

Add these methods to `AnsibleMysqlDeployer` (near `deploy_full_repl`),
reusing the existing `SequenceStepError` pattern from
`_run_full_repl_sequence`:

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
        limit="vm5",
        extra_vars=self._build_extra_vars(),
    )
    if not results["reinstall_primary"]["success"]:
        raise SequenceStepError("reinstall_primary step failed; see results.reinstall_primary for details", results)

    return results

def get_rewind_plan(self) -> Dict[str, object]:
    """Static description of each destructive playbook -- not a live dry-run,
    since these are shell/mysql/xtrabackup tasks that Ansible --check can't safely simulate."""
    return {
        "note": "Describes what each playbook does; not a live check-mode run.",
        "teardown": {
            "playbook": "teardown.yml",
            "endpoint": "POST /api/v1/deploy/teardown",
            "leaves_mysql_installed": True,
            "steps": [
                "Stop replica threads and RESET REPLICA ALL on vm6",
                "Wipe vm6's datadir and reinitialize it as a bare MySQL instance, root password re-secured",
                "Drop the replication user on vm5",
                "Remove XtraBackup artifacts on both hosts",
                "Clear the controller-side backup relay directory",
            ],
        },
        "rewind": {
            "playbooks": ["teardown.yml", "site.yml (limit vm5)"],
            "endpoint": "POST /api/v1/deploy/rewind",
            "leaves_mysql_installed": True,
            "steps": [
                "Everything in teardown, then",
                "Re-run site.yml against vm5 only: reconfirm config and recreate the replication user",
                "Leaves vm5 ready and vm6 as a bare instance -- ready to retry backup/replication",
            ],
        },
        "reset-baseline": {
            "playbook": "uninstall.yml",
            "endpoint": "POST /api/v1/deploy/reset-baseline",
            "leaves_mysql_installed": False,
            "steps": [
                "Stop mysqld",
                "Remove MySQL and Percona XtraBackup packages and their yum repos",
                "Delete data_dir, log_dir, backup_dir, and the lab replication config file",
                "Clear the controller-side backup relay directory",
                "Both hosts return to a bare VM -- call POST /api/v1/deploy/install to rebuild",
            ],
        },
    }
```

### 6. Edit: `python-fastapi-mysql/app/routes/deploy.py`

Add four routes (same try/except/logging shape as the existing ones):

```python
@router.post("/teardown")
async def deploy_teardown(background_tasks: BackgroundTasks):
    """Tear down MySQL replication and backup artifacts.

    Leaves MySQL installed and running on both hosts. Safe to re-run.
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
            "estimated_duration_minutes": 5,
        }
    except Exception as e:
        logger.error(f"Error initiating teardown: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate teardown: {str(e)}")


@router.post("/rewind")
async def deploy_rewind(background_tasks: BackgroundTasks):
    """Tear down replication/backup artifacts, then rebuild a clean vm5.

    Leaves vm6 as a bare MySQL instance -- ready to retry
    POST /deploy/backup then /deploy/replication from a clean primary.
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
            "estimated_duration_minutes": 10,
        }
    except Exception as e:
        logger.error(f"Error initiating rewind: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate rewind: {str(e)}")


@router.post("/reset-baseline")
async def deploy_reset_baseline(background_tasks: BackgroundTasks):
    """Uninstall MySQL entirely and return both hosts to a bare VM.

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
            "estimated_duration_minutes": 5,
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

### 7. Edit: `python-fastapi-mysql/tests/test_api.py`

Same fixture shape as the other two services (`TestClient(app)` via a
`client` fixture). **Do not** add a test that calls
`client.post("/api/v1/deploy/teardown")` (or `/rewind`, `/reset-baseline`)
directly — same reason as always: `TestClient` runs `BackgroundTasks`
synchronously inside the request, which would genuinely shell out to
`ansible-playbook` during `pytest`.

```python
def test_rewind_plan_endpoint(client):
    """GET-only, no side effects, safe to call in CI."""
    response = client.get("/api/v1/deploy/rewind-plan")
    assert response.status_code == 200
    data = response.json()
    assert "teardown" in data and "rewind" in data and "reset-baseline" in data


def test_deploy_rewind_sequence_stops_on_teardown_failure(monkeypatch):
    """Rewind must not attempt site.yml if teardown itself fails."""
    from app.deployer import AnsibleMysqlDeployer, SequenceStepError

    deployer = AnsibleMysqlDeployer()
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

- [`mysql-fastapi-build-from-scratch.md`](mysql-fastapi-build-from-scratch.md)
  **Part 9 — Rerun it**: replace the paragraph starting "There's no
  teardown/rewind/reset-baseline playbook for this build yet" with a
  pointer to this document, the same way the Oracle build guide's Part 9
  now points at its own teardown/rewind doc.
- Once a `python-fastapi-mysql/CHANGELOG.md`/`RUNBOOK.md` exist (they
  don't yet) — add the teardown/rewind/reset-baseline addition there too.

## Commands to run it yourself

All from `python-fastapi-mysql/` unless noted, on VM1 (the controller).

**1. Syntax-check the two new playbooks before touching real state:**
```bash
cd python-fastapi-mysql
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
uvicorn app.main:app --reload --host 0.0.0.0 --port 8002
```

**4. Exercise each endpoint individually and watch the log:**
```bash
curl http://localhost:8002/api/v1/deploy/rewind-plan | jq
curl -X POST http://localhost:8002/api/v1/deploy/teardown
tail -f logs/ansible.log        # watch it run
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'
```

**5. The real proof — reset to bare VM, then rebuild straight through with no manual fixes:**
```bash
# Wipe both VMs back to bare state
curl -X POST http://localhost:8002/api/v1/deploy/reset-baseline
tail -f logs/ansible.log     # wait for it to finish; check history for status=success
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'

# Rebuild everything in one call: install MySQL, snapshot+restore, start replication
curl -X POST http://localhost:8002/api/v1/deploy/full-repl
tail -f logs/ansible.log
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'
```

**6. Independently verify replication is healthy** (don't just trust the playbook's own output):
```bash
# on vm6
mysql -u root -p'<mysql_root_password>' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source"
```
Expect `Replica_IO_Running: Yes`, `Replica_SQL_Running: Yes`,
`Seconds_Behind_Source: 0`.

**7. Practice a mid-cycle rewind** (tear down replication only, keep MySQL installed on both hosts, retry from a clean primary):
```bash
curl -X POST http://localhost:8002/api/v1/deploy/rewind
tail -f logs/ansible.log
curl -X POST http://localhost:8002/api/v1/deploy/backup
curl -X POST http://localhost:8002/api/v1/deploy/replication
```

## Verification checklist

- [ ] `--syntax-check` passes on both new playbooks
- [ ] New pytest cases pass, no real `ansible-playbook` subprocess triggered by them
- [ ] `GET /rewind-plan` returns the three sections with no side effects
- [ ] `POST /teardown` on a host with active replication succeeds; re-running it immediately after (nothing left to tear down) also succeeds (idempotency)
- [ ] `POST /reset-baseline` leaves `systemctl status mysqld` reporting the unit as not-found/inactive and `/var/lib/mysql` gone on both VMs
- [ ] `POST /full-repl` after `reset-baseline` reaches `Replica_IO_Running: Yes` / `Replica_SQL_Running: Yes` / `Seconds_Behind_Source: 0` with no manual SSH intervention
- [ ] `POST /rewind` after working replication leaves vm5 with the `repl` user recreated and vm6 as a bare instance, confirmed via `mysql -e "SHOW REPLICA STATUS\G"` on vm6 returning empty (no configured source) and `mysql -e "SELECT user FROM mysql.user WHERE user='repl'"` on vm5 returning one row
