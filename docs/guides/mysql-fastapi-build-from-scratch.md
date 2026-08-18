# Building the MySQL 8.4 XtraBackup Replication FastAPI + Ansible Service From Scratch

> A from-zero build plan for a new service, `python-fastapi-mysql/`, that
> mirrors the shape of
> [`mssql-fastapi-build-from-scratch.md`](mssql-fastapi-build-from-scratch.md)
> and
> [`oracle-fastapi-build-from-scratch.md`](oracle-fastapi-build-from-scratch.md):
> FastAPI driving its own embedded Ansible tree, this time installing MySQL
> Community Edition 8.4.10 on two fresh VMs, taking a hot Percona XtraBackup
> snapshot of the primary, restoring it onto the replica, and starting
> GTID-based replication between them. It follows the phases laid out in
> [`mysql-8-4-10-xtrabackup-replication-design.md`](mysql-8-4-10-xtrabackup-replication-design.md).
>
> **This is a plan to review, not a record of a working build** — same
> caveat as the Oracle guide. Nothing here has run against `devops_VM5`/
> `devops_VM6` yet. The single most likely thing to need adjusting on the
> first real run is exact package/repo naming (MySQL's Yum subrepo name for
> 8.4, Percona's XtraBackup package name for 8.4 compatibility) — flagged
> inline below, not glossed over.

## What you're building

```
curl -> FastAPI (uvicorn) -> AnsibleRunner (subprocess) -> ansible-playbook -> vm5 + vm6
```

One FastAPI process, running on the **existing** `devops_VM1` — reusing it
as the Ansible controller, the same way the Oracle build does. Passwordless
SSH from VM1 to both VM5 and VM6 is already in place, plus `devops` sudoers
on both, so there's no controller-side SSH setup to do here either.
`python-fastapi-mysql/` lives alongside `python-fastapi-mssql/` and
`python-fastapi-oracle-dg/` in the same repo, on the same VM1 — a new,
independent project directory that touches neither of them. It listens on
port `8002` (MSSQL has `8000`, Oracle has `8001`) so all three can run at
once on the same host.

## Prerequisites & lab topology

| VM | Role | Hostname | IP |
|---|---|---|---|
| SQL/Ansible VM 1 | Ansible controller (reused) | `devops_VM1` | `192.168.70.129` |
| MySQL VM 5 | Primary | `devops_VM5` | `192.168.70.133` |
| MySQL VM 6 | Replica | `devops_VM6` | `192.168.70.134` |

- VM1 needs nothing new beyond what the other two services already
  required (Python 3.9+, `pip`) — the `mysql_repl` role never targets VM1.
- VM5/VM6: already up, `devops` user configured with SSH key auth and
  passwordless sudo from VM1 (per your setup) — Part 1 below just verifies
  this rather than creating it from scratch.
- Assume the same OS family as the rest of the lab (RHEL-compatible 8.x)
  unless you tell the role otherwise — `prep`/`install` use `yum` and
  `firewalld`/`systemd` accordingly.
- **Minimum sizing**: MySQL itself is far lighter than Oracle — 2 GB RAM /
  2 vCPU / 20 GB free disk is comfortable for a lab-sized dataset. No
  swap/hugepage tuning needed the way the Oracle guide required.
- **Package/repo naming is the one thing here that's genuinely uncertain**,
  unlike AdventureWorks' public `get_url` or Oracle's manually-staged zip:
  MySQL's Yum repository package enables version-specific "subrepos" (e.g.
  `mysql84-lts-community`) that Oracle periodically restructures across
  releases, and Percona names XtraBackup packages by MySQL compatibility
  (`percona-xtrabackup-84` is this guide's best-effort guess for 8.4
  compatibility — verify against `percona-release list` on the actual VM
  before trusting it). Both are called out again inline where they matter.

---
## Part 1 — Verify SSH access

Keys and sudo are already set up per-VM, passwordless from VM1 — this is
purely a verification step, run **from VM1**:

```bash
ssh -i ~/.ssh/id_rsa devops@192.168.70.133 'hostname && sudo -n true && echo SUDO_OK'   # vm5
ssh -i ~/.ssh/id_rsa devops@192.168.70.134 'hostname && sudo -n true && echo SUDO_OK'   # vm6
```

Both should print their hostname and `SUDO_OK`. Same as the Oracle guide's
Part 1: VM1 is controller-only here, never a member of `mysql_servers`, so
there's no "authorize the controller against itself" step to do.

## Part 2 — Project skeleton

On VM1, alongside the existing `python-fastapi-mssql/` and
`python-fastapi-oracle-dg/` directories:

```bash
mkdir -p python-fastapi-mysql/{app/routes,ansible/inventory,ansible/playbooks,ansible/roles/mysql_repl/{tasks,defaults,handlers,templates},tests,logs,backups}
cd python-fastapi-mysql
```

No `software/` directory this time, unlike the Oracle build — MySQL
Community Server and Percona XtraBackup both install from public Yum
repositories, no manually-downloaded install media to stage. `templates/`
is still needed, for `my.cnf`.

---
## Part 3 — The Ansible role: `ansible/roles/mysql_repl/`

### `defaults/main.yml`

```yaml
---
# Default variables for embedded MySQL replication role

mysql_version: "8.4.10"
mysql_repo_rpm_url: "https://dev.mysql.com/get/mysql80-community-release-el8-9.noarch.rpm"
mysql_yum_subrepo_disable: "mysql80-community"
mysql_yum_subrepo_enable: "mysql84-lts-community"

percona_repo_rpm_url: "https://repo.percona.com/yum/percona-release-latest.noarch.rpm"
percona_xtrabackup_package: "percona-xtrabackup-84"

mysql_root_password: "MySQLStr0ng!Passw0rd"
mysql_repl_user: "repl"
mysql_repl_password: "ReplStr0ng!Passw0rd"

mysql_port: 3306
data_dir: "/var/lib/mysql"
log_dir: "/var/log/mysql"
backup_dir: "/backup/xtrabackup"
local_backup_dir: "./backups/vm5_xtrabackup"

innodb_buffer_pool_size: "512M"
server_id: 1   # always overridden per-host by the inventory hostvar of the same name
```

`server_id` is deliberately given a throwaway default — every real run
supplies it per-host from inventory (Part 4). It's the same "fixed identity
that outlives a role change" concept as the Oracle guide's `db_unique_name`:
vm5 is always `server_id=1` and vm6 is always `server_id=2`, regardless of
which one is currently accepting writes — MySQL replication topology
doesn't actually change which server_id belongs to which host the way a
promotion might (this build doesn't even implement a promotion/failover
path yet — see the Open Questions at the end).

### `templates/my.cnf.j2`

```ini
[mysqld]
server-id = {{ server_id }}
port = {{ mysql_port }}
datadir = {{ data_dir }}
socket = /var/lib/mysql/mysql.sock
log-error = {{ log_dir }}/mysqld.log
pid-file = /var/run/mysqld/mysqld.pid

# Binary logging + GTID -- required for XtraBackup's auto-position replica init
log_bin = mysql-bin
binlog_format = ROW
gtid_mode = ON
enforce_gtid_consistency = ON
log_replica_updates = ON

innodb_buffer_pool_size = {{ innodb_buffer_pool_size }}

bind-address = 0.0.0.0
```

`log_replica_updates` is 8.4's current name for what used to be
`log_slave_updates` — MySQL renamed the master/slave terminology across the
whole product surface starting in 8.0.23 (`CHANGE MASTER TO` →
`CHANGE REPLICATION SOURCE TO`, `START SLAVE` → `START REPLICA`,
`SHOW SLAVE STATUS` → `SHOW REPLICA STATUS`, and so on) — by 8.4 the new
names are the primary ones, not just aliases, so this guide uses them
throughout rather than the terms most existing tutorials online still use.
Rendered onto **both** hosts identically — replication direction is
runtime state (which side is running `START REPLICA`), not something baked
into the config file.

### `tasks/install.yml`

```yaml
---
# Install MySQL Community Server 8.4 + Percona XtraBackup, then bootstrap
# the root password while the server is still using its packaged default
# my.cnf (and therefore its default log location) -- configure.yml (next)
# only runs after this, so it's safe for it to redirect logging without
# racing this file's password-bootstrap step.

- name: Install yum-utils (needed for yum-config-manager)
  yum:
    name: yum-utils
    state: present
  tags:
    - install

- name: Install the MySQL Yum repository package
  yum:
    name: "{{ mysql_repo_rpm_url }}"
    state: present
    disable_gpg_check: yes
  tags:
    - install
    - repo

- name: Switch the enabled MySQL subrepo from 8.0 to 8.4 LTS
  command: "yum-config-manager --disable {{ mysql_yum_subrepo_disable }} --enable {{ mysql_yum_subrepo_enable }}"
  changed_when: true
  tags:
    - install
    - repo

- name: Install the Percona repository package
  yum:
    name: "{{ percona_repo_rpm_url }}"
    state: present
    disable_gpg_check: yes
  tags:
    - install
    - repo

- name: Enable the Percona XtraBackup LTS tool repo
  command: percona-release setup -y pxb-innovation-lts
  changed_when: true
  tags:
    - install
    - repo

- name: Install MySQL Community Server and client (pinned to mysql_version)
  yum:
    name:
      - "mysql-community-server-{{ mysql_version }}"
      - "mysql-community-client-{{ mysql_version }}"
    state: present
  tags:
    - install
    - mysql

- name: Install Percona XtraBackup
  yum:
    name: "{{ percona_xtrabackup_package }}"
    state: present
  tags:
    - install
    - xtrabackup

- name: Enable and start mysqld with its packaged default config
  systemd:
    name: mysqld
    enabled: yes
    state: started
  tags:
    - install
    - service

- name: Wait for MySQL to start
  wait_for:
    port: "{{ mysql_port }}"
    delay: 5
    timeout: 60
  tags:
    - install
    - service

- name: Check whether root password bootstrap has already run
  stat:
    path: /root/.mysql_bootstrapped
  register: bootstrap_marker
  tags:
    - install

- name: Extract the temporary root password from the default install log
  shell: |
    grep 'temporary password' /var/log/mysqld.log | tail -1 | sed 's/.*root@localhost: //'
  register: temp_root_password
  changed_when: false
  when: not bootstrap_marker.stat.exists
  tags:
    - install

- name: Set the real root password and relax the default password policy for lab use
  shell: |
    mysql --connect-expired-password -u root -p'{{ temp_root_password.stdout }}' -e "
    ALTER USER 'root'@'localhost' IDENTIFIED BY '{{ mysql_root_password }}';
    UNINSTALL COMPONENT 'file://component_validate_password';
    "
  when: not bootstrap_marker.stat.exists
  changed_when: true
  tags:
    - install

- name: Mark root password bootstrap complete
  file:
    path: /root/.mysql_bootstrapped
    state: touch
    mode: '0600'
  when: not bootstrap_marker.stat.exists
  tags:
    - install

- name: Verify MySQL is reachable with the real root password
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SELECT VERSION();"
  register: version_check
  changed_when: false
  tags:
    - install
    - verify

- name: Display MySQL version
  debug:
    var: version_check.stdout
  tags:
    - install
    - verify
```

`UNINSTALL COMPONENT 'file://component_validate_password'` is the same
"keep it simple for a lab" call the Oracle guide made with SELinux —
`validate_password` is genuinely useful in production, but its complexity
rules would otherwise get in the way of a lab that's about replication
mechanics, not password policy. `--connect-expired-password` is what lets
the very first authenticated command be the `ALTER USER` itself — without
it, MySQL refuses every other statement (including `UNINSTALL COMPONENT`)
until the password is changed, so both have to happen in the same
connection/statement batch.

### `tasks/configure.yml`

```yaml
---
# Point MySQL at lab-managed directories/settings and open the firewall.

- name: Create log directory
  file:
    path: "{{ log_dir }}"
    state: directory
    owner: mysql
    group: mysql
    mode: '0750'
  tags:
    - configure
    - directories

- name: Create backup directory
  file:
    path: "{{ backup_dir }}"
    state: directory
    owner: mysql
    group: mysql
    mode: '0750'
  tags:
    - configure
    - directories

- name: Render lab replication config (separate file, doesn't touch the packaged /etc/my.cnf)
  template:
    src: my.cnf.j2
    dest: /etc/my.cnf.d/lab-replication.cnf
    owner: root
    group: root
    mode: '0644'
  notify: restart mysqld
  tags:
    - configure

- name: Flush handlers to apply configuration changes
  meta: flush_handlers
  tags:
    - configure

- name: Check firewalld status
  command: systemctl is-active firewalld
  register: firewalld_status
  failed_when: false
  changed_when: false
  tags:
    - configure
    - firewall

- name: Open MySQL port in firewalld
  firewalld:
    port: "{{ mysql_port }}/tcp"
    permanent: yes
    immediate: yes
    state: enabled
  when: firewalld_status.stdout | trim == 'active'
  tags:
    - configure
    - firewall
```

`/etc/my.cnf.d/lab-replication.cnf` is a dedicated file rather than
overwriting the package-provided `/etc/my.cnf` directly — RHEL/OL's MySQL
packaging already includes `/etc/my.cnf.d/*.cnf` via `!includedir` in the
base file, so this is the standard, upgrade-safe way to layer config on
top rather than fighting the package for ownership of one file.

### `tasks/replication_user.yml` (vm5 only)

```yaml
---
# Create the replication account on the primary. MySQL's DDL supports
# IF NOT EXISTS natively for CREATE USER, so this doesn't need the
# check-then-branch shape the MSSQL/Oracle guides use for the same idea.

- name: Create the replication user and grant REPLICATION SLAVE
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    CREATE USER IF NOT EXISTS '{{ mysql_repl_user }}'@'%' IDENTIFIED BY '{{ mysql_repl_password }}';
    GRANT REPLICATION SLAVE ON *.* TO '{{ mysql_repl_user }}'@'%';
    FLUSH PRIVILEGES;
    "
  when: inventory_hostname == 'vm5'
  changed_when: true
  tags:
    - replication_user
```

---
### `tasks/backup.yml` (vm5 only)

```yaml
---
# Take a hot XtraBackup snapshot of the primary and prepare it (apply redo
# logs) so it's restorable. Runs on vm5 only.

- name: Reset the backup directory (safe to re-run)
  file:
    path: "{{ backup_dir }}"
    state: "{{ item }}"
    owner: mysql
    group: mysql
    mode: '0750'
  loop:
    - absent
    - directory
  when: inventory_hostname == 'vm5'
  tags:
    - backup

- name: Take the XtraBackup snapshot
  shell: |
    xtrabackup --backup --target-dir={{ backup_dir }} \
      --user=root --password='{{ mysql_root_password }}' \
      --datadir={{ data_dir }}
  when: inventory_hostname == 'vm5'
  changed_when: true
  tags:
    - backup

- name: Prepare the backup (apply redo logs, make it consistent)
  shell: |
    xtrabackup --prepare --target-dir={{ backup_dir }}
  when: inventory_hostname == 'vm5'
  changed_when: true
  tags:
    - backup

- name: Verify the backup's binlog/GTID position file exists
  stat:
    path: "{{ backup_dir }}/xtrabackup_binlog_info"
  register: binlog_info_stat
  when: inventory_hostname == 'vm5'
  tags:
    - backup
    - verify

- name: Assert the GTID position file was captured
  assert:
    that:
      - binlog_info_stat.stat.exists
    fail_msg: "xtrabackup_binlog_info not found in {{ backup_dir }} -- backup did not complete correctly."
  when: inventory_hostname == 'vm5'
  tags:
    - backup
    - verify

- name: Display the captured binlog/GTID position
  shell: "cat {{ backup_dir }}/xtrabackup_binlog_info"
  register: binlog_info_display
  when: inventory_hostname == 'vm5'
  changed_when: false
  tags:
    - backup
    - verify

- name: Show binlog info
  debug:
    msg: "{{ binlog_info_display.stdout }}"
  when: inventory_hostname == 'vm5'
  tags:
    - backup
    - verify
```

`xtrabackup_binlog_info` is the whole reason this build can use GTID
auto-position replication without ever manually reading a binlog file/offset
off the primary — XtraBackup writes the exact GTID set that was `SHOW
MASTER STATUS`-consistent at the moment the backup was taken directly into
this file, as the last field of a single tab-separated line (binlog file,
position, GTID set). `restore.yml` reads it straight off disk.

### `tasks/restore.yml` (controller relay + vm6 only)

Same controller-relay shape as the MSSQL build's backup-stripe transfer and
the Oracle build's password-file relay — `synchronize: mode: pull` off vm5,
plain `copy` onto vm6, both delegated through the controller (VM1).

```yaml
---
# Relay the prepared backup from vm5 to vm6 via the controller, restore it,
# and set gtid_purged from the backup's captured GTID set.

- name: Ensure local relay directory exists on controller
  file:
    path: "{{ local_backup_dir }}"
    state: directory
    mode: '0755'
  delegate_to: localhost
  become: false
  when: inventory_hostname == "vm5"
  tags:
    - restore
    - transfer

- name: Fetch the prepared backup from vm5 to the controller
  synchronize:
    src: "{{ backup_dir }}/"
    dest: "{{ local_backup_dir }}/"
    mode: pull
  when: inventory_hostname == "vm5"
  tags:
    - restore
    - transfer

- name: Copy the prepared backup to vm6
  copy:
    src: "{{ local_backup_dir }}/"
    dest: "{{ backup_dir }}/"
    owner: mysql
    group: mysql
    mode: '0640'
  when: inventory_hostname == "vm6"
  tags:
    - restore
    - transfer

- name: Verify the backup arrived on vm6
  stat:
    path: "{{ backup_dir }}/xtrabackup_binlog_info"
  register: binlog_info_stat_vm6
  when: inventory_hostname == "vm6"
  tags:
    - restore
    - verify

- name: Assert the backup's GTID position file is present on vm6
  assert:
    that:
      - binlog_info_stat_vm6.stat.exists
    fail_msg: "xtrabackup_binlog_info not found in {{ backup_dir }} on vm6 -- transfer did not complete."
  when: inventory_hostname == "vm6"
  tags:
    - restore
    - verify

- name: Stop mysqld on vm6 before restoring
  systemd:
    name: mysqld
    state: stopped
  when: inventory_hostname == "vm6"
  tags:
    - restore

- name: Clear the existing (empty, default-initialized) datadir on vm6
  file:
    path: "{{ data_dir }}"
    state: "{{ item }}"
    owner: mysql
    group: mysql
    mode: '0750'
  loop:
    - absent
    - directory
  when: inventory_hostname == "vm6"
  tags:
    - restore

- name: Restore the prepared backup into the datadir
  shell: |
    xtrabackup --copy-back --target-dir={{ backup_dir }} --datadir={{ data_dir }}
  when: inventory_hostname == "vm6"
  changed_when: true
  tags:
    - restore

- name: Fix ownership of the restored datadir
  file:
    path: "{{ data_dir }}"
    owner: mysql
    group: mysql
    recurse: yes
  when: inventory_hostname == "vm6"
  tags:
    - restore

- name: Start mysqld on vm6 with the restored data
  systemd:
    name: mysqld
    state: started
  when: inventory_hostname == "vm6"
  tags:
    - restore

- name: Wait for MySQL to start on vm6
  wait_for:
    host: "{{ ansible_host }}"
    port: "{{ mysql_port }}"
    delay: 5
    timeout: 60
  when: inventory_hostname == "vm6"
  tags:
    - restore

- name: Extract the GTID set captured at backup time (last field of xtrabackup_binlog_info)
  shell: "awk '{print $NF}' {{ backup_dir }}/xtrabackup_binlog_info"
  register: captured_gtid_set
  when: inventory_hostname == "vm6"
  changed_when: false
  tags:
    - restore

- name: Set gtid_purged on vm6 to match the backup's known-good position
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    RESET BINARY LOGS AND GTIDS;
    SET GLOBAL gtid_purged = '{{ captured_gtid_set.stdout }}';
    "
  when: inventory_hostname == "vm6" and (captured_gtid_set.stdout | trim) != ''
  changed_when: true
  tags:
    - restore
```

`RESET BINARY LOGS AND GTIDS` is 8.4's current name for the old `RESET
MASTER` — same terminology overhaul as `log_replica_updates`. The
`awk '{print $NF}'` extraction assumes `xtrabackup_binlog_info` is a single
tab/space-separated line ending in the GTID set, which is the documented
format with GTID mode on — worth confirming against the file's real
contents on the first run, same "verify against real output" caveat as
everywhere else uncertain in this series. Clearing the datadir before
`--copy-back` matters because `mysqld --initialize` (which the package's
first-start hook already ran during `install.yml`) leaves a small default
data directory behind — `xtrabackup --copy-back` refuses to write into a
non-empty datadir.

### `tasks/replication.yml` (vm6 only)

```yaml
---
# Point vm6 at vm5 and start GTID auto-position replication. Runs on vm6 only.

- name: Configure the replication source (GTID auto-position -- no manual log file/position needed)
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "
    CHANGE REPLICATION SOURCE TO
      SOURCE_HOST='{{ hostvars['vm5'].ansible_host }}',
      SOURCE_PORT={{ mysql_port }},
      SOURCE_USER='{{ mysql_repl_user }}',
      SOURCE_PASSWORD='{{ mysql_repl_password }}',
      SOURCE_AUTO_POSITION=1;
    "
  when: inventory_hostname == 'vm6'
  changed_when: true
  tags:
    - replication

- name: Start replica threads
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "START REPLICA;"
  when: inventory_hostname == 'vm6'
  changed_when: true
  tags:
    - replication

- name: Wait for replica threads to report healthy
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running"
  register: replica_status
  retries: 12
  delay: 5
  until: "'Replica_IO_Running: Yes' in replica_status.stdout and 'Replica_SQL_Running: Yes' in replica_status.stdout"
  changed_when: false
  when: inventory_hostname == 'vm6'
  tags:
    - replication
    - verify

- name: Display full replica status
  shell: |
    mysql -u root -p'{{ mysql_root_password }}' -e "SHOW REPLICA STATUS\G"
  register: replica_status_full
  when: inventory_hostname == 'vm6'
  changed_when: false
  tags:
    - replication
    - verify

- name: Show replica status
  debug:
    msg: "{{ replica_status_full.stdout_lines }}"
  when: inventory_hostname == 'vm6'
  tags:
    - replication
    - verify
```

`SOURCE_AUTO_POSITION=1` is the payoff of the whole GTID + `gtid_purged`
setup above — with it, MySQL negotiates exactly where to resume replication
from the GTID sets each side already knows about, no `SOURCE_LOG_FILE`/
`SOURCE_LOG_POS` to compute by hand the way non-GTID replication would need.

### `tasks/main.yml`

```yaml
---
# Main task file for embedded mysql_repl role

- name: Include install tasks
  include_tasks: install.yml
  tags:
    - always

- name: Include configure tasks
  include_tasks: configure.yml
  tags:
    - always

- name: Include replication user tasks
  include_tasks: replication_user.yml
  tags:
    - always

- name: Include backup tasks
  include_tasks: backup.yml
  tags:
    - always

- name: Include restore tasks
  include_tasks: restore.yml
  tags:
    - always

- name: Include replication tasks
  include_tasks: replication.yml
  tags:
    - always
```

### `handlers/main.yml`

```yaml
---
# MySQL replication role handlers

- name: restart mysqld
  systemd:
    name: mysqld
    state: restarted
```

---
## Part 4 — Playbooks, inventory, and `ansible.cfg`

### `ansible/inventory/hosts.ini`

```ini
[mysql_servers]
vm5 ansible_host=192.168.70.133 vmware_name=devops_VM5 server_id=1 mysql_role=primary
vm6 ansible_host=192.168.70.134 vmware_name=devops_VM6 server_id=2 mysql_role=replica

[mysql_servers:vars]
ansible_user=devops
ansible_ssh_private_key_file=/home/devops/.ssh/id_rsa
ansible_connection=ssh
ansible_port=22
```

Same naming convention as the MSSQL and Oracle inventories — `vmware_name`
carried over for consistency even though nothing in this role needs it yet;
`server_id` is the one genuinely new per-host identity var this role
requires.

### `ansible/playbooks/site.yml`

```yaml
---
- name: Deploy MySQL Community Server and the replication user
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  pre_tasks:
    - name: Display deployment information
      debug:
        msg: |
          Deploying MySQL {{ mysql_version }} to {{ inventory_hostname }}
          Role: {{ mysql_role }}
          IP Address: {{ ansible_host }}
          OS: {{ ansible_os_family }}
      tags:
        - always

    - name: Validate system requirements
      assert:
        that:
          - ansible_os_family == "RedHat"
          - ansible_distribution_major_version == "8"
        fail_msg: "This role requires a RHEL-compatible 8.x host"
      tags:
        - always

  tasks:
    - name: Run install, configure, and replication-user tasks
      include_role:
        name: mysql_repl
        tasks_from: "{{ item }}"
      loop:
        - install.yml
        - configure.yml
        - replication_user.yml
      tags:
        - site

  post_tasks:
    - name: Create summary report
      copy:
        content: |
          MySQL Deployment Report
          ================================
          Deployment Date: {{ ansible_date_time.iso8601 }}
          Host: {{ inventory_hostname }}
          Role: {{ mysql_role }}
          MySQL Version: {{ mysql_version }}
          Status: SUCCESS
        dest: /tmp/mysql_deployment_{{ inventory_hostname }}.txt
      tags:
        - always
```

Runs on **both** hosts — `install.yml`/`configure.yml` apply to both;
`replication_user.yml` internally no-ops everywhere except vm5. By the end
of `site.yml`: both hosts have MySQL 8.4 running with GTID/binlog enabled,
and vm5 has a `repl` user granted `REPLICATION SLAVE`.

### `ansible/playbooks/backup.yml`

```yaml
---
- name: Take an XtraBackup snapshot of the primary and restore it on the replica
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Show backup/restore status
      debug:
        msg: "Processing {{ inventory_hostname }} for backup/restore"

    - name: Run backup tasks
      include_role:
        name: mysql_repl
        tasks_from: backup.yml
      tags:
        - backup

    - name: Run restore tasks
      include_role:
        name: mysql_repl
        tasks_from: restore.yml
      tags:
        - backup
```

Deliberately **not** pre-filtered by host at the playbook level, unlike the
MSSQL build's own `backup.yml`, which gates `include_role tasks_from:
restore.yml` to `when: inventory_hostname == "vm2"` — combined with that
file's internal `vm1`-gated controller-fetch step, that combination would
leave the fetch unreachable in a real run. This playbook instead includes
both task files unconditionally on the whole group and lets their own
internal per-task `when: inventory_hostname == ...` guards do the actual
host-scoping — which is how `backup.yml`/`restore.yml` above are written to
be run, and the same pattern the Oracle guides use throughout.

### `ansible/playbooks/replication.yml`

```yaml
---
- name: Configure and start GTID replication from vm5 to vm6
  hosts: mysql_servers
  become: yes
  gather_facts: yes

  pre_tasks:
    - name: Verify MySQL connectivity on each host
      shell: |
        mysql -u root -p'{{ mysql_root_password }}' -e "SELECT 1;"
      register: connectivity_check
      retries: 10
      delay: 5
      until: connectivity_check.rc == 0
      changed_when: false
      tags:
        - replication

  tasks:
    - name: Configure replication
      include_role:
        name: mysql_repl
        tasks_from: replication.yml
      tags:
        - replication
```

### `ansible.cfg` (in `python-fastapi-mysql/`, next to `app/`)

```ini
[defaults]
log_path = ./logs/ansible.log
host_key_checking = False
```

Identical to the other two services' `ansible.cfg` — persists every run's
output to `logs/ansible.log`, resolves relative to wherever `uvicorn` is
started from, so always launch it from `python-fastapi-mysql/`.

---
## Part 5 — The FastAPI service

This part is the closest parallel of the three DB services to the original
MSSQL one: MySQL's `site` → `backup` → `replication` three-stage sequence
maps almost one-to-one onto MSSQL's `site` → `backup` → `alwayson`, right
down to the same `full-*` combined endpoint shape. Oracle needed a fourth
stage (`standby` split out from `dataguard`) because RMAN duplication and
broker configuration are different enough failure domains to separate; MySQL's
XtraBackup restore and `START REPLICA` are simple enough to combine the way
MSSQL's backup/restore already are.

### `requirements.txt`

```
fastapi==0.109.0
uvicorn[standard]==0.27.0
pydantic==2.5.3
pydantic-settings==2.1.0
paramiko==3.4.0
psutil==5.9.6
python-multipart==0.0.6
ansible-core==2.13.13
pytest==7.4.3
pytest-asyncio==0.21.1
httpx==0.25.2
```

### `app/__init__.py` / `app/routes/__init__.py`

```python
"""App package initialization"""
```
```python
"""Routes package initialization"""
```

### `app/config.py`

```python
"""Configuration module"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import os


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    # Application
    APP_NAME: str = "MySQL Replication Deployment API"
    VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"

    # MySQL configuration
    MYSQL_ROOT_PASSWORD: str = os.getenv("MYSQL_ROOT_PASSWORD", "MySQLStr0ng!Passw0rd")
    MYSQL_REPL_PASSWORD: str = os.getenv("MYSQL_REPL_PASSWORD", "ReplStr0ng!Passw0rd")
    MYSQL_VERSION: str = os.getenv("MYSQL_VERSION", "8.4.10")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))

    # VMware NAT target addresses
    VM5_HOST: str = os.getenv("VM5_HOST", "192.168.70.133")
    VM6_HOST: str = os.getenv("VM6_HOST", "192.168.70.134")
    VM5_USER: str = os.getenv("VM5_USER", "devops")
    VM6_USER: str = os.getenv("VM6_USER", "devops")

    # SSH configuration
    SSH_PORT: int = int(os.getenv("SSH_PORT", "22"))
    SSH_KEY_PATH: str = os.getenv("SSH_KEY_PATH", "~/.ssh/id_rsa")
    SSH_TIMEOUT: int = int(os.getenv("SSH_TIMEOUT", "30"))

    # Backup relay
    BACKUP_DIR: str = os.getenv("BACKUP_DIR", "/backup/xtrabackup")
    LOCAL_BACKUP_DIR: str = os.getenv("LOCAL_BACKUP_DIR", "./backups/vm5_xtrabackup")

    # Ansible configuration
    ANSIBLE_INVENTORY: str = os.getenv("ANSIBLE_INVENTORY", "./ansible/inventory/hosts.ini")
    ANSIBLE_PLAYBOOK_DIR: str = os.getenv("ANSIBLE_PLAYBOOK_DIR", "./ansible/playbooks")
    ANSIBLE_CMD: str = os.getenv("ANSIBLE_CMD", "ansible-playbook")
    ANSIBLE_VERBOSE: int = int(os.getenv("ANSIBLE_VERBOSE", "1"))
    ANSIBLE_PRIVATE_KEY_FILE: str = os.getenv("ANSIBLE_PRIVATE_KEY_FILE", "~/.ssh/id_rsa")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: str = os.getenv("LOG_DIR", "./logs")

    # API
    API_TIMEOUT: int = int(os.getenv("API_TIMEOUT", "1800"))  # 30 minutes -- MySQL builds fast

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value):
        """Handle common non-boolean DEBUG values from host environments."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).lower() in {"1", "true", "yes", "on", "debug", "development"}

settings = Settings()
```

`API_TIMEOUT` is the shortest of the three services — no silent installer,
no RMAN active-database duplication over the network, no `dbca`. A hot
XtraBackup snapshot plus copy-back of a lab-sized dataset comfortably fits
in well under 30 minutes.

### `app/ansible_runner.py`

Identical to the MSSQL and Oracle services' file — copy it as-is, nothing
in it is product-specific. See
[`mssql-fastapi-build-from-scratch.md` Part 5](mssql-fastapi-build-from-scratch.md#part-5--the-fastapi-service)
for the full listing.

### `app/deployer.py`

```python
"""Deployment orchestration wrapper for FastAPI."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from app.ansible_runner import AnsibleRunner
from app.config import settings


logger = logging.getLogger(__name__)


class SequenceStepError(RuntimeError):
    """Raised when one step of a multi-playbook sequence fails, carrying the partial results."""

    def __init__(self, message: str, results: Dict[str, object]) -> None:
        super().__init__(message)
        self.results = results


class AnsibleMysqlDeployer:
    """Deploy MySQL Community Server + XtraBackup replication using Ansible playbooks."""

    def __init__(self) -> None:
        self.ansible = AnsibleRunner()
        self._history: List[Dict] = []
        self._lock = threading.Lock()

    def start_task(self, operation: str) -> str:
        task_id = str(uuid.uuid4())
        self._record(
            {
                "task_id": task_id,
                "operation": operation,
                "status": "queued",
                "started_at": datetime.now().isoformat(),
                "completed_at": None,
                "results": None,
                "error": None,
            }
        )
        return task_id

    def get_history(self) -> List[Dict]:
        with self._lock:
            return list(self._history)

    def _record(self, record: Dict) -> None:
        with self._lock:
            self._history.insert(0, record)

    def _update(self, task_id: str, **changes) -> None:
        with self._lock:
            task = next(item for item in self._history if item["task_id"] == task_id)
            task.update(changes)

    def _run_task(self, task_id: str, action) -> None:
        self._update(task_id, status="running")
        try:
            result = action()
            self._update(
                task_id,
                status="success",
                completed_at=datetime.now().isoformat(),
                results=result,
            )
        except Exception as exc:
            logger.exception("Deployment task failed")
            self._update(
                task_id,
                status="failed",
                completed_at=datetime.now().isoformat(),
                error=str(exc),
                results=getattr(exc, "results", None),
            )

    def deploy_site(self, task_id: str) -> None:
        self._run_task(task_id, lambda: self.ansible.run_playbook("site.yml", extra_vars=self._build_extra_vars()))

    def deploy_backup_restore(self, task_id: str) -> None:
        self._run_task(task_id, lambda: self.ansible.run_playbook("backup.yml", extra_vars=self._build_extra_vars()))

    def deploy_replication(self, task_id: str) -> None:
        self._run_task(task_id, lambda: self.ansible.run_playbook("replication.yml", extra_vars=self._build_extra_vars()))

    def deploy_full_repl(self, task_id: str) -> None:
        self._run_task(task_id, self._run_full_repl_sequence)

    def _run_full_repl_sequence(self) -> Dict[str, object]:
        results: Dict[str, object] = {}
        results["site"] = self.ansible.run_playbook("site.yml", extra_vars=self._build_extra_vars())
        if not results["site"]["success"]:
            raise SequenceStepError("site step failed; see results.site for details", results)

        results["backup_restore"] = self.ansible.run_playbook("backup.yml", extra_vars=self._build_extra_vars())
        if not results["backup_restore"]["success"]:
            raise SequenceStepError("backup_restore step failed; see results.backup_restore for details", results)

        results["replication"] = self.ansible.run_playbook("replication.yml", extra_vars=self._build_extra_vars())
        if not results["replication"]["success"]:
            raise SequenceStepError("replication step failed; see results.replication for details", results)

        return results

    def get_hosts(self) -> Dict:
        return {
            "hosts": [
                {"name": "vm5", "host": settings.VM5_HOST, "user": settings.VM5_USER, "port": settings.SSH_PORT, "role": "primary"},
                {"name": "vm6", "host": settings.VM6_HOST, "user": settings.VM6_USER, "port": settings.SSH_PORT, "role": "replica"},
            ]
        }

    def resolve_hosts(self) -> Dict:
        import socket

        results = {}
        for hostname in [settings.VM5_HOST, settings.VM6_HOST]:
            try:
                results[hostname] = {
                    "address": socket.gethostbyname(hostname),
                    "resolved": True,
                }
            except OSError as exc:
                results[hostname] = {
                    "resolved": False,
                    "error": str(exc),
                }
        return results

    def ping_hosts(self) -> Dict:
        import socket

        results = []
        for host in [settings.VM5_HOST, settings.VM6_HOST]:
            try:
                with socket.create_connection((host, settings.SSH_PORT), timeout=settings.SSH_TIMEOUT):
                    results.append({"host": host, "status": "reachable"})
            except Exception as exc:
                results.append({"host": host, "status": "unreachable", "error": str(exc)})
        return {
            "status": "success" if all(r["status"] == "reachable" for r in results) else "failed",
            "results": results,
        }

    def _build_extra_vars(self) -> Dict[str, object]:
        return {
            "mysql_root_password": settings.MYSQL_ROOT_PASSWORD,
            "mysql_repl_password": settings.MYSQL_REPL_PASSWORD,
            "mysql_version": settings.MYSQL_VERSION,
            "mysql_port": settings.MYSQL_PORT,
            "backup_dir": settings.BACKUP_DIR,
            "local_backup_dir": settings.LOCAL_BACKUP_DIR,
        }
```

Same `SequenceStepError` shape as the other two services — a failed `site`
step stops before `backup_restore`, a failed `backup_restore` stops before
`replication` ever tries to point a replica at a database that was never
actually restored.

---
### `app/routes/deploy.py`

```python
"""Deployment routes"""
from fastapi import APIRouter, HTTPException, status, BackgroundTasks
import logging
from app.config import settings
from app.deployer import AnsibleMysqlDeployer

router = APIRouter()
logger = logging.getLogger(__name__)

deployer = AnsibleMysqlDeployer()


@router.get("/status")
async def get_deployment_status():
    """Get current deployment status"""
    history = deployer.get_history()
    latest = history[0] if history else None
    return {
        "status": latest["status"] if latest else "ready",
        "latest_task": latest,
        "engine": "ansible",
        "mysql_version": settings.MYSQL_VERSION,
        "vm5": settings.VM5_HOST,
        "vm6": settings.VM6_HOST,
    }


@router.post("/install")
async def deploy_install(background_tasks: BackgroundTasks):
    """Install MySQL Community Server on both hosts and create the replication user.

    Long-running (5-15 minutes). Returns immediately with a task ID.
    """
    logger.info("Received deployment request - Install MySQL (site.yml)")
    try:
        task_id = deployer.start_task("site")
        background_tasks.add_task(deployer.deploy_site, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "MySQL installation started",
            "engine": "ansible",
            "playbook": "site.yml",
            "estimated_duration_minutes": 10,
            "instructions": "Check /api/v1/deploy/status or /api/v1/deploy/history for progress",
        }
    except Exception as e:
        logger.error(f"Error initiating deployment: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate deployment: {str(e)}")


@router.post("/backup")
async def deploy_backup(background_tasks: BackgroundTasks):
    """Take an XtraBackup snapshot of vm5 and restore it onto vm6.

    Prerequisites: /install must have completed successfully first.
    """
    logger.info("Received deployment request - Backup and restore")
    try:
        task_id = deployer.start_task("backup-restore")
        background_tasks.add_task(deployer.deploy_backup_restore, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Backup and restore started",
            "engine": "ansible",
            "playbook": "backup.yml",
            "operations": [
                "xtrabackup --backup + --prepare on vm5",
                "Relay the prepared backup to vm6 via the controller",
                "xtrabackup --copy-back on vm6, set gtid_purged",
            ],
            "estimated_duration_minutes": 10,
        }
    except Exception as e:
        logger.error(f"Error initiating backup: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate backup: {str(e)}")


@router.post("/replication")
async def deploy_replication(background_tasks: BackgroundTasks):
    """Configure GTID auto-position replication from vm5 to vm6 and start it."""
    logger.info("Received deployment request - Configure replication")
    try:
        task_id = deployer.start_task("replication")
        background_tasks.add_task(deployer.deploy_replication, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Replication configuration started",
            "engine": "ansible",
            "playbook": "replication.yml",
            "estimated_duration_minutes": 5,
            "operations": [
                "Verify MySQL connectivity",
                "CHANGE REPLICATION SOURCE TO ... SOURCE_AUTO_POSITION=1",
                "START REPLICA",
                "Verify Replica_IO_Running / Replica_SQL_Running",
            ],
        }
    except Exception as e:
        logger.error(f"Error initiating replication configuration: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate replication configuration: {str(e)}")


@router.post("/full-repl")
async def deploy_full_repl(background_tasks: BackgroundTasks):
    """Run the whole build in sequence: install, backup/restore, configure replication."""
    logger.info("Received deployment request - Full replication workflow")
    try:
        task_id = deployer.start_task("full-repl")
        background_tasks.add_task(deployer.deploy_full_repl, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Full MySQL replication build started",
            "engine": "ansible",
            "playbooks": ["site.yml", "backup.yml", "replication.yml"],
            "operations": [
                "Install MySQL 8.4 + XtraBackup on vm5 and vm6, create replication user",
                "Snapshot vm5 with XtraBackup, relay and restore onto vm6",
                "Configure and start GTID replication from vm5 to vm6",
            ],
            "estimated_duration_minutes": 20,
        }
    except Exception as e:
        logger.error(f"Error initiating full replication workflow: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate full replication workflow: {str(e)}")


@router.get("/history")
async def get_deployment_history():
    """Get deployment execution history"""
    try:
        history = deployer.get_history()
        return {"total_executions": len(history), "executions": history}
    except Exception as e:
        logger.error(f"Error retrieving history: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve history: {str(e)}")


@router.get("/hosts")
async def get_target_hosts():
    """Get target host information"""
    try:
        inventory = deployer.get_hosts()
        return {
            "status": "success",
            "inventory": inventory,
            "dns": deployer.resolve_hosts(),
            "ansible_inventory": settings.ANSIBLE_INVENTORY,
            "ansible_playbooks": settings.ANSIBLE_PLAYBOOK_DIR,
        }
    except Exception as e:
        logger.error(f"Error retrieving hosts: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve hosts: {str(e)}")


@router.post("/ping")
async def ping_hosts():
    """Ping all target hosts to verify connectivity"""
    try:
        return deployer.ping_hosts()
    except Exception as e:
        logger.error(f"Error pinging hosts: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to ping hosts: {str(e)}")
```

Four write endpoints — `install`, `backup`, `replication`, plus the
combined `full-repl` — the exact same count and shape as MSSQL's
`install`/`backup`/`alwayson`/`full-ag`.

### `app/routes/health.py` and `app/routes/logs.py`

Copy both files verbatim from the MSSQL or Oracle service — neither is
product-specific. See
[`mssql-fastapi-build-from-scratch.md` Part 5](mssql-fastapi-build-from-scratch.md#part-5--the-fastapi-service)
for the listing. Same caveat applies here too: `health.py`'s
`GET /api/v1/health/ready` checks for Paramiko/an SSH key, not MySQL or
Ansible reachability — use `GET /api/v1/deploy/hosts` and
`POST /api/v1/deploy/ping` for a real readiness signal.

### `app/main.py`

```python
"""
MySQL Replication FastAPI Deployment Service
Main application entry point
"""

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
import logging
from pathlib import Path

# Import routers
from app.routes import deploy, health, logs
from app.config import settings

# Logging configuration
Path(settings.LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(Path(settings.LOG_DIR) / "app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="MySQL Replication Deployment API",
    description="FastAPI service for MySQL 8.4 + XtraBackup replication automation using embedded Ansible playbooks",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# Include routers
app.include_router(deploy.router, prefix="/api/v1/deploy", tags=["deployment"])
app.include_router(health.router, prefix="/api/v1/health", tags=["health"])
app.include_router(logs.router, prefix="/api/v1/logs", tags=["logs"])


@app.get("/", tags=["root"])
async def root():
    """Root endpoint - API information"""
    return {
        "service": "MySQL Replication Deployment API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/api/docs",
        "endpoints": {
            "deploy": "/api/v1/deploy",
            "health": "/api/v1/health",
            "logs": "/api/v1/logs",
        },
    }


@app.get("/api/v1/", tags=["api"])
async def api_root():
    """API v1 root"""
    return {
        "version": "1.0.0",
        "endpoints": {
            "deploy": "/api/v1/deploy",
            "health": "/api/v1/health",
            "logs": "/api/v1/logs",
        },
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
```

Port `8002` — MSSQL owns `8000`, Oracle owns `8001`, so this defaults to
the next one, all three able to run at once on VM1.

### `.env`

```bash
DEBUG=False
LOG_LEVEL=INFO

MYSQL_ROOT_PASSWORD=MySQLStr0ng!Passw0rd
MYSQL_REPL_PASSWORD=ReplStr0ng!Passw0rd
MYSQL_VERSION=8.4.10
MYSQL_PORT=3306

VM5_HOST=192.168.70.133
VM6_HOST=192.168.70.134
VM5_USER=devops
VM6_USER=devops

SSH_PORT=22
SSH_KEY_PATH=/home/devops/.ssh/id_rsa
SSH_TIMEOUT=30

ANSIBLE_INVENTORY=./ansible/inventory/hosts.ini
ANSIBLE_PLAYBOOK_DIR=./ansible/playbooks
ANSIBLE_CMD=ansible-playbook
ANSIBLE_PRIVATE_KEY_FILE=/home/devops/.ssh/id_rsa
ANSIBLE_VERBOSE=1

BACKUP_DIR=/backup/xtrabackup
LOCAL_BACKUP_DIR=./backups/vm5_xtrabackup

API_TIMEOUT=1800
```

Change both passwords before any real use — `MYSQL_ROOT_PASSWORD` and
`MYSQL_REPL_PASSWORD` are the two secrets this build creates.

---
## Part 6 — Install and run

```bash
cd python-fastapi-mysql
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

uvicorn app.main:app --reload --host 0.0.0.0 --port 8002
```

Confirm it's up:
```bash
curl http://localhost:8002/api/v1/health/check
```

## Part 7 — Build it: the CLI way

**1. Confirm both VMs are reachable:**
```bash
curl -X POST http://localhost:8002/api/v1/deploy/ping
curl http://localhost:8002/api/v1/deploy/hosts | jq
```

**2. Run the whole build in one call** — installs MySQL on both hosts,
snapshots vm5 with XtraBackup, restores onto vm6, and starts GTID
replication:
```bash
curl -X POST http://localhost:8002/api/v1/deploy/full-repl
tail -f logs/ansible.log
```

**3. Or run it step by step:**
```bash
curl -X POST http://localhost:8002/api/v1/deploy/install       # site.yml: both VMs, repl user on vm5
curl -X POST http://localhost:8002/api/v1/deploy/backup        # backup.yml: XtraBackup snapshot + restore
curl -X POST http://localhost:8002/api/v1/deploy/replication   # replication.yml: CHANGE REPLICATION SOURCE + START REPLICA
```

**4. Check progress/result at any point:**
```bash
curl http://localhost:8002/api/v1/deploy/status | jq
curl http://localhost:8002/api/v1/deploy/history | jq '.executions[0]'
```

**5. Verify independently** — don't just trust the playbook's own `debug`
output:
```bash
# on vm6
mysql -u root -p'<mysql_root_password>' -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source"
```
Expect `Replica_IO_Running: Yes`, `Replica_SQL_Running: Yes`, and
`Seconds_Behind_Source: 0`.

## Part 8 — Build it: the Swagger UI way

1. Open `http://<vm1-ip>:8002/api/docs` in a browser (or
   `http://localhost:8002/api/docs` if you're on VM1 itself).
2. Expand **deployment → POST /api/v1/deploy/ping**, **Try it out**,
   **Execute**. Confirm both hosts come back `"reachable"`.
3. Expand **POST /api/v1/deploy/full-repl**, **Try it out**, **Execute**
   with an empty body. The response shows `"status": "initiated"` and a
   `task_id` immediately — the Ansible run continues in the background.
4. Poll the same way as the other two services: **GET /api/v1/deploy/status**
   or **/history**, **Execute** again periodically.
5. **health** and **logs** are safe to click any time (all read-only `GET`s
   except `POST /logs/clear`).

## Part 9 — Rerun it

**If both VMs are still bare**, rerunning is identical to Part 7/8:
`POST /deploy/full-repl` again.

**If replication is already built and healthy**, calling `full-repl` again
will **not** cleanly rebuild it: `backup.yml`'s restore step stops `mysqld`
and wipes vm6's live datadir unconditionally — running it against an
already-replicating pair destroys the replica's current (correct) data
just to re-copy the same thing, and `replication.yml`'s
`CHANGE REPLICATION SOURCE TO` while replica threads are already running
will simply fail until they're stopped first. Unlike the Oracle build (whose
`full-dg` turned out to be accidentally idempotent end to end), this one is
much closer to the MSSQL build's `full-ag`: safe on a bare pair, destructive
on a healthy one.

Tearing down MySQL replication cleanly (stop replica threads, reset
`gtid_purged`, drop the replication user, remove the datadir, uninstall the
packages) is covered in
[`mysql-rewind-and-teardown-implementation.md`](mysql-rewind-and-teardown-implementation.md),
the same way the MSSQL and Oracle build guides were each followed by a
dedicated teardown/rewind guide rather than including one inline. That doc
adds `POST /deploy/teardown`, `/rewind`, and `/reset-baseline` — once
they're in place, the clean-rerun sequence is:
```bash
curl -X POST http://localhost:8002/api/v1/deploy/reset-baseline   # wipe both VMs
curl -X POST http://localhost:8002/api/v1/deploy/full-repl         # rebuild from bare
```

---

## Open questions to confirm before you run this

Because nothing here has been tested against VM5/VM6 yet, these are the
places most likely to need a tweak once you do — roughly in order of how
likely each is to actually bite:

1. **Yum/Percona repo and package names.** `mysql84-lts-community` and
   `percona-xtrabackup-84` are this guide's best-effort names for 8.4
   compatibility — both are exactly the kind of thing Oracle/Percona
   restructure across releases. Before trusting either, run
   `yum repolist all | grep mysql` and `percona-release list` on the real
   VM and adjust `defaults/main.yml` to match what's actually available.
2. **Exact 8.4.10 availability.** If the Yum repo's current snapshot
   doesn't have exactly `8.4.10` (patch releases roll forward), pinning
   `mysql-community-server-{{ mysql_version }}` will fail outright rather
   than silently installing something else — check
   `yum list --showduplicates mysql-community-server` first, or relax the
   pin to `mysql-community-server: state=latest` if exact-version pinning
   isn't important to you.
3. **`xtrabackup_binlog_info`'s exact format.** The `awk '{print $NF}'`
   extraction in `restore.yml` assumes the documented tab-separated
   `file, position, GTID-set` layout — confirm against the real file's
   contents on the first run before trusting `gtid_purged` gets set
   correctly.
4. **OS package availability for `qpress`/compression.** This build doesn't
   use XtraBackup's compression options (`--compress`) at all, so it
   doesn't need `qpress` — if you want compressed backups for a bigger
   dataset later, that's an additional package and an extra
   `xtrabackup --decompress` step before `--prepare`, not covered here.
5. **`validate_password` removal.** Uninstalling the component (Part 3,
   `install.yml`) is a lab-convenience choice, not a technical requirement
   — if you'd rather keep MySQL's default password policy, drop that line
   and make sure `MYSQL_ROOT_PASSWORD`/`MYSQL_REPL_PASSWORD` satisfy it
   before the `ALTER USER` step runs, or it'll fail on the very first
   deploy.

---
