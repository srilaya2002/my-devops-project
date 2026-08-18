# Building the Oracle 19c Data Guard FastAPI + Ansible Service From Scratch

> A from-zero build plan for a new service, `python-fastapi-oracle-dg/`, that
> mirrors the shape of the working MSSQL service
> ([`mssql-fastapi-build-from-scratch.md`](mssql-fastapi-build-from-scratch.md)):
> FastAPI driving its own embedded Ansible tree, this time installing Oracle
> 19c on two fresh VMs, creating a primary database, duplicating it to a
> physical standby, and wiring the pair into a Data Guard configuration
> managed by the broker (`dgmgrl`). It follows the phases laid out in
> [`oracle-19c-dataguard-design.md`](oracle-19c-dataguard-design.md).
>
> **This is a plan to review, not a record of a working build.** Unlike the
> MSSQL guide — which documents code that's live and already fixed several
> real bugs from testing — nothing here has been run against `devops_VM3` /
> `devops_VM4` yet. Package names, exact silent-install response-file keys,
> and timing numbers are correct to the best of standard Oracle 19c practice,
> but treat the first real run as the actual test — expect to hit at least a
> couple of environment-specific snags (kernel parameters, swap size, a
> missing RPM) the same way the MSSQL build did, and to feed fixes back into
> this doc once you have them.

## What you're building

```
curl -> FastAPI (uvicorn) -> AnsibleRunner (subprocess) -> ansible-playbook -> vm3 + vm4
```

One FastAPI process, running on the **existing** `devops_VM1` — reusing it
as the Ansible controller for this build too, rather than standing up a
second controller on VM3. Passwordless SSH from VM1 to both VM3 and VM4 is
already in place, so there's no controller-side SSH work to do here at all
(contrast with the MSSQL build, where VM1 also had to be authorized against
*itself* since it doubles as a deployment target — VM3/VM4 don't have that
wrinkle, because VM1 is controller-only here and is never itself a target
of the `oracle_dg` role). `python-fastapi-oracle-dg/` lives alongside
`python-fastapi-mssql/` in the same repo, on the same VM1 — a new,
independent project directory that does not touch `python-fastapi-mssql/`
or `ansible-mssql-deploy/` in any way, and can be built and run entirely
separately from them. It listens on port `8001` specifically so it can run
side by side with the MSSQL service's `8000` on the same host.

## Prerequisites & lab topology

| VM | Role | Hostname | IP |
|---|---|---|---|
| SQL/Ansible VM 1 | Ansible controller (reused) | `devops_VM1` | `192.168.70.129` |
| Oracle VM 3 | Primary | `devops_VM3` | `192.168.70.131` |
| Oracle VM 4 | Standby | `devops_VM4` | `192.168.70.132` |

- VM1 needs nothing new installed for this build beyond what
  `python-fastapi-mssql/` already required (Python 3.9+, `pip`) — the
  `oracle_dg` role never targets VM1, so it never needs an Oracle Home,
  kernel-parameter changes, or `firewalld` rules of its own. It's purely
  the box `ansible-playbook` runs from.
- VM3/VM4: already up, `devops` user configured with SSH key auth and
  passwordless sudo from VM1 (per your setup) — Part 1 below just verifies
  this rather than creating it from scratch.
- Assume the same OS family as VM3/VM4 that VM1/VM2 already use
  (RHEL-compatible 8.x) unless you tell the role otherwise — the prep tasks
  below use `yum`/`dnf` and `firewalld`/`systemd` accordingly. If VM3/VM4
  are actually a different major version, the package list in `prep.yml`
  and the `ansible_distribution_major_version` assertion in `site.yml` are
  the two places to adjust first.
- **Minimum sizing for Oracle 19c** (both VMs): 8 GB RAM (16 GB more
  comfortable — `dbca` will warn below ~8 GB), 2+ vCPU, at least 40 GB free
  under `/u01` for software + database files, swap >= RAM up to 16 GB per
  Oracle's installation guide. Check before starting:
  ```bash
  free -h
  df -h /
  ```
- **Oracle installation media — cannot be automated end to end.** Unlike the
  MSSQL build (`get_url` pulls a public GitHub release), the Oracle 19c
  database software zip (`LINUX.X64_193000_db_home.zip`) requires an
  authenticated Oracle account to download and Oracle's license terms don't
  allow scripting that download. You'll need to:
  1. Download `LINUX.X64_193000_db_home.zip` yourself from Oracle (My Oracle
     Support / Oracle Software Delivery Cloud), and
  2. Place it at `python-fastapi-oracle-dg/software/LINUX.X64_193000_db_home.zip`
     on the controller (VM1, next to where you'll run `uvicorn` from) before
     running the install playbook.
  The Ansible role stages it from there onto both VM3 and VM4
  (`stage_software.yml`, Part 3) — the same controller-relay pattern the
  MSSQL build already uses for backup stripes and AG certificates, just
  relaying the install media outbound to both hosts instead of a backup
  inbound from one.

---
## Part 1 — Verify SSH access

Keys and sudo are already set up, and passwordless SSH from VM1 to both
VM3 and VM4 already exists per your setup — this step is purely a
verification, run **from VM1**, that it actually works before Ansible
relies on it:

```bash
ssh -i ~/.ssh/id_rsa devops@192.168.70.131 'hostname && sudo -n true && echo SUDO_OK'   # vm3
ssh -i ~/.ssh/id_rsa devops@192.168.70.132 'hostname && sudo -n true && echo SUDO_OK'   # vm4
```

Both should print their hostname and `SUDO_OK`. Unlike the MSSQL build's
Part 1, there's no "authorize the controller against itself" step here —
VM1 is controller-only for this build and is never a member of the
`oracle_servers` inventory group, so it's never an SSH target of its own
playbook runs.

## Part 2 — Project skeleton

On VM1, alongside the existing `python-fastapi-mssql/` directory:

```bash
mkdir -p python-fastapi-oracle-dg/{app/routes,ansible/inventory,ansible/playbooks,ansible/roles/oracle_dg/{tasks,defaults,handlers,templates},tests,logs,software,backups}
cd python-fastapi-oracle-dg
```

`software/` is new relative to the MSSQL skeleton — it's where you drop the
Oracle install zip from the prerequisites above. `templates/` is also new:
the MSSQL role never needed Jinja2 templates because `sqlcmd -Q "..."`
inlines everything; Oracle's silent installers and `dbca`/`netca` want real
response files on disk, so this role renders `listener.ora`, `tnsnames.ora`,
`db_install.rsp`, and `dbca.rsp` from templates.

---
## Part 3 — The Ansible role: `ansible/roles/oracle_dg/`

### `defaults/main.yml`

```yaml
---
# Default variables for embedded Oracle Data Guard role

oracle_version: "19.3.0.0.0"
oracle_zip_name: "LINUX.X64_193000_db_home.zip"
local_software_dir: "./software"
oracle_stage_dir: "/u01/stage"

oracle_user: "oracle"
oracle_group: "oinstall"
oracle_extra_groups:
  - dba
  - oper
oracle_base: "/u01/app/oracle"
oracle_home: "/u01/app/oracle/product/19.0.0/dbhome_1"
oracle_inventory_dir: "/u01/app/oraInventory"

db_name: "orcl"
primary_db_unique_name: "orcl_p"
standby_db_unique_name: "orcl_s"
oracle_sid: "orcl"
oracle_pdb_name: "orclpdb1"
listener_name: "LISTENER"
listener_port: 1521

oracle_pwd: "OracleStr0ng!Passw0rd"          # SYS/SYSTEM password, also DG broker password
character_set: "AL32UTF8"
db_storage_type: "FS"                          # filesystem storage, not ASM -- simplest for a 2-node lab
db_data_dir: "/u01/app/oracle/oradata"
db_fra_dir: "/u01/app/oracle/fast_recovery_area"
db_fra_size_mb: 20480
redo_log_size_mb: 200
standby_redo_log_count: 4                       # primary's own log count + 1, standard DG sizing rule

dg_config_name: "orcl_dg"
dg_protection_mode: "MaxPerformance"            # async, no dedicated network -- see design doc
local_cert_relay_dir: "./backups/oracle_dg_relay"   # password file + tnsnames relay, same pattern as MSSQL AG certs

oracle_required_packages:
  - binutils
  - compat-openssl10
  - elfutils-libelf
  - elfutils-libelf-devel
  - fontconfig
  - glibc
  - glibc-devel
  - ksh
  - libaio
  - libaio-devel
  - libX11
  - libXau
  - libXi
  - libXtst
  - libXrender
  - libgcc
  - libnsl
  - libstdc++
  - libstdc++-devel
  - libxcb
  - make
  - net-tools
  - nfs-utils
  - policycoreutils
  - policycoreutils-python-utils
  - smartmontools
  - sysstat
  - unzip
  - zip
```

`db_storage_type: FS` keeps this lab on plain filesystem storage rather than
ASM — matches the design doc's "keep it simple for clarity" choice and means
no extra ASM instance/diskgroup setup on either VM. `dg_protection_mode:
MaxPerformance` is async redo shipping — the only mode that doesn't require
`SYNC` transport and doesn't stall primary commits if the standby or network
blips, appropriate for a lab with a single network path between VM3 and VM4.

### `tasks/prep.yml`

```yaml
---
# OS-level prep: groups, user, directories, kernel params, packages
# Runs on both vm3 and vm4 -- both need a full Oracle Home, standby included.

- name: Install required OS packages
  yum:
    name: "{{ oracle_required_packages }}"
    state: present
  tags:
    - prep
    - packages

- name: Create oinstall group
  group:
    name: "{{ oracle_group }}"
    state: present
  tags:
    - prep
    - users

- name: Create dba/oper groups
  group:
    name: "{{ item }}"
    state: present
  loop: "{{ oracle_extra_groups }}"
  tags:
    - prep
    - users

- name: Create oracle user
  user:
    name: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    groups: "{{ oracle_extra_groups | join(',') }}"
    append: yes
    shell: /bin/bash
    create_home: yes
  tags:
    - prep
    - users

- name: Set oracle user password-less for lab convenience (login via su - oracle only, no direct SSH)
  user:
    name: "{{ oracle_user }}"
    password: "{{ oracle_pwd | password_hash('sha512') }}"
  tags:
    - prep
    - users

- name: Create Oracle base/home/inventory directories
  file:
    path: "{{ item }}"
    state: directory
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0775'
  loop:
    - "{{ oracle_base }}"
    - "{{ oracle_home }}"
    - "{{ oracle_inventory_dir }}"
    - "{{ db_data_dir }}"
    - "{{ db_fra_dir }}"
    - "{{ oracle_stage_dir }}"
  tags:
    - prep
    - directories

- name: Apply kernel parameters for Oracle 19c
  sysctl:
    name: "{{ item.name }}"
    value: "{{ item.value }}"
    sysctl_set: yes
    state: present
    reload: yes
  loop:
    - { name: "fs.file-max", value: "6815744" }
    - { name: "kernel.sem", value: "250 32000 100 128" }
    - { name: "kernel.shmmni", value: "4096" }
    - { name: "kernel.shmall", value: "1073741824" }
    - { name: "kernel.shmmax", value: "4398046511104" }
    - { name: "kernel.panic_on_oops", value: "1" }
    - { name: "net.core.rmem_default", value: "262144" }
    - { name: "net.core.rmem_max", value: "4194304" }
    - { name: "net.core.wmem_default", value: "262144" }
    - { name: "net.core.wmem_max", value: "1048576" }
    - { name: "fs.aio-max-nr", value: "1048576" }
    - { name: "net.ipv4.ip_local_port_range", value: "9000 65500" }
  tags:
    - prep
    - kernel

- name: Set oracle user resource limits
  pam_limits:
    domain: "{{ oracle_user }}"
    limit_type: "{{ item.type }}"
    limit_item: "{{ item.item }}"
    value: "{{ item.value }}"
  loop:
    - { type: 'soft', item: 'nofile', value: '1024' }
    - { type: 'hard', item: 'nofile', value: '65536' }
    - { type: 'soft', item: 'nproc',  value: '16384' }
    - { type: 'hard', item: 'nproc',  value: '16384' }
    - { type: 'soft', item: 'stack',  value: '10240' }
    - { type: 'hard', item: 'stack',  value: '32768' }
  tags:
    - prep
    - kernel

- name: Disable transparent huge pages at boot (grub2, RHEL8 pattern)
  lineinfile:
    path: /etc/default/grub
    regexp: '^GRUB_CMDLINE_LINUX='
    line: 'GRUB_CMDLINE_LINUX="crashkernel=auto rhgb quiet transparent_hugepage=never"'
    backup: yes
  register: grub_updated
  tags:
    - prep
    - kernel

- name: Regenerate grub config if changed
  command: grub2-mkconfig -o /boot/grub2/grub.cfg
  when: grub_updated.changed
  tags:
    - prep
    - kernel

- name: Warn that a reboot is needed for THP/grub changes to take effect
  debug:
    msg: >-
      grub.cfg was updated to disable transparent huge pages. This host needs
      a reboot before continuing past prep.yml for the setting to apply --
      Oracle's installer only warns about THP, it doesn't block on it, so you
      can continue without rebooting immediately, but reboot before going to
      production-like load testing.
  when: grub_updated.changed
  tags:
    - prep
    - kernel

- name: Check firewalld status
  command: systemctl is-active firewalld
  register: firewalld_status
  failed_when: false
  changed_when: false
  tags:
    - prep
    - firewall

- name: Open Oracle listener port in firewalld
  firewalld:
    port: "{{ listener_port }}/tcp"
    permanent: yes
    immediate: yes
    state: enabled
  when: firewalld_status.stdout | trim == 'active'
  tags:
    - prep
    - firewall

- name: Set SELinux to permissive (lab simplification, matches design doc's "keep it simple" choice)
  selinux:
    policy: targeted
    state: permissive
  tags:
    - prep
    - selinux
```

The kernel/limits values above are Oracle's own documented 19c minimums
(`Oracle Database Installation Guide for Linux`, preinstall chapter) — the
same numbers the `oracle-database-preinstall-19c` RPM would set if it were
available for this OS; since that RPM is Oracle-Linux-specific and VM3/VM4
are assumed RHEL-compatible, `prep.yml` sets them by hand instead. SELinux
`permissive` (rather than `disabled`) is deliberately the lighter-touch
choice — it logs denials without blocking, which is enough to keep a lab
install from failing without turning SELinux off at the boot-loader level.

---
### `tasks/stage_software.yml`

```yaml
---
# Relay the Oracle install zip from the controller to both VMs and unzip it.
# Same controller-relay shape as the MSSQL build's backup-stripe/cert transfer,
# just outbound to both hosts instead of pulled from one.

- name: Verify the install zip exists on the controller before doing anything
  stat:
    path: "{{ local_software_dir }}/{{ oracle_zip_name }}"
  delegate_to: localhost
  become: false
  run_once: true
  register: local_zip_stat
  tags:
    - stage
    - software

- name: Fail early with a clear message if the zip wasn't staged
  assert:
    that:
      - local_zip_stat.stat.exists
    fail_msg: >-
      {{ local_software_dir }}/{{ oracle_zip_name }} not found on the
      controller. Download it from Oracle and place it there first --
      see the Prerequisites section, this can't be automated.
  run_once: true
  tags:
    - stage
    - software

- name: Check whether the zip is already staged on this host
  stat:
    path: "{{ oracle_stage_dir }}/{{ oracle_zip_name }}"
  register: remote_zip_stat
  tags:
    - stage
    - software

- name: Copy install zip to this host
  copy:
    src: "{{ local_software_dir }}/{{ oracle_zip_name }}"
    dest: "{{ oracle_stage_dir }}/{{ oracle_zip_name }}"
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0644'
  when: not remote_zip_stat.stat.exists
  tags:
    - stage
    - software

- name: Check whether the zip has already been extracted into ORACLE_HOME
  stat:
    path: "{{ oracle_home }}/bin/sqlplus"
  register: sqlplus_stat
  tags:
    - stage
    - software

- name: Extract install zip into ORACLE_HOME
  unarchive:
    src: "{{ oracle_stage_dir }}/{{ oracle_zip_name }}"
    dest: "{{ oracle_home }}"
    remote_src: yes
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
  when: not sqlplus_stat.stat.exists
  tags:
    - stage
    - software
```

### `templates/db_install.rsp.j2`

```ini
oracle.install.responseFileVersion=/oracle/install/rspfmt_dbinstall_response_schema_v19.0.0
oracle.install.option=INSTALL_DB_SWONLY
ORACLE_HOSTNAME={{ ansible_fqdn | default(ansible_hostname) }}
UNIX_GROUP_NAME={{ oracle_group }}
INVENTORY_LOCATION={{ oracle_inventory_dir }}
SELECTED_LANGUAGES=en
ORACLE_HOME={{ oracle_home }}
ORACLE_BASE={{ oracle_base }}
oracle.install.db.InstallEdition=EE
oracle.install.db.OSDBA_GROUP=dba
oracle.install.db.OSOPER_GROUP=oper
oracle.install.db.OSBACKUPDBA_GROUP=dba
oracle.install.db.OSDGDBA_GROUP=dba
oracle.install.db.OSKMDBA_GROUP=dba
oracle.install.db.OSRACDBA_GROUP=dba
SECURITY_UPDATES_VIA_MYORACLESUPPORT=false
DECLINE_SECURITY_UPDATES=true
```

`INSTALL_DB_SWONLY` — software-only install, no database created here.
`dbca` creates the primary database as its own separate step (next file),
and the standby never goes through `dbca` at all — it's populated entirely
by RMAN duplication in `duplicate_standby.yml`.

### `tasks/install_software.yml`

```yaml
---
# Silent software-only install + root scripts. Runs on both vm3 and vm4.

- name: Render db_install.rsp
  template:
    src: db_install.rsp.j2
    dest: "{{ oracle_stage_dir }}/db_install.rsp"
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0644'
  tags:
    - install
    - software

- name: Check whether software is already installed (inventory has a home registered)
  stat:
    path: "{{ oracle_inventory_dir }}/ContentsXML/inventory.xml"
  register: inventory_stat
  tags:
    - install
    - software

- name: Run Oracle universal installer (silent, software-only)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/runInstaller -silent -waitforcompletion \
      -responseFile {{ oracle_stage_dir }}/db_install.rsp \
      -ignorePrereqFailure
  register: installer_result
  failed_when: installer_result.rc not in [0, 6]   # runInstaller returns 6 when root scripts are still pending -- expected
  when: not inventory_stat.stat.exists
  tags:
    - install
    - software

- name: Run orainstRoot.sh
  command: "{{ oracle_inventory_dir }}/orainstRoot.sh"
  when: not inventory_stat.stat.exists
  tags:
    - install
    - software
    - root-scripts

- name: Run root.sh
  command: "{{ oracle_home }}/root.sh"
  when: not inventory_stat.stat.exists
  tags:
    - install
    - software
    - root-scripts

- name: Verify sqlplus is present after install
  stat:
    path: "{{ oracle_home }}/bin/sqlplus"
  register: sqlplus_check
  tags:
    - install
    - verify

- name: Assert install succeeded
  assert:
    that:
      - sqlplus_check.stat.exists
    fail_msg: "{{ oracle_home }}/bin/sqlplus not found -- runInstaller did not complete successfully."
  tags:
    - install
    - verify

- name: Set oracle user's shell environment (.bash_profile)
  blockinfile:
    path: "/home/{{ oracle_user }}/.bash_profile"
    marker: "# {mark} ANSIBLE MANAGED BLOCK - oracle env"
    block: |
      export ORACLE_BASE={{ oracle_base }}
      export ORACLE_HOME={{ oracle_home }}
      export ORACLE_SID={{ oracle_sid }}
      export PATH=$ORACLE_HOME/bin:$PATH
      export LD_LIBRARY_PATH=$ORACLE_HOME/lib:$LD_LIBRARY_PATH
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
  tags:
    - install
    - env
```

`-ignorePrereqFailure` matches the lab-simplification stance already taken
for SELinux/THP above — real production installs should fix prereq
warnings, not skip them, but this build is explicitly a learning lab per the
design doc's stated purpose. `failed_when: rc not in [0, 6]` exists because
`runInstaller -silent` legitimately exits `6` when it finishes but still has
root scripts pending — that isn't a failure, `root.sh` right after handles it.

### `templates/listener.ora.j2`

```ini
LISTENER =
  (DESCRIPTION_LIST =
    (DESCRIPTION =
      (ADDRESS = (PROTOCOL = TCP)(HOST = {{ ansible_host }})(PORT = {{ listener_port }}))
      (ADDRESS = (PROTOCOL = IPC)(KEY = EXTPROC1))
    )
  )

SID_LIST_LISTENER =
  (SID_LIST =
    (SID_DESC =
      (GLOBAL_DBNAME = {{ db_unique_name }}_DGMGRL)
      (SID_NAME = {{ oracle_sid }})
      (ORACLE_HOME = {{ oracle_home }})
    )
  )

ADR_BASE_LISTENER = {{ oracle_base }}
```

The static `SID_LIST_LISTENER` entry with a `_DGMGRL` global name is
required on **both** hosts, before either instance is even up — the broker
needs to be able to reach an instance for role transitions (switchover/
failover) via a static service registration, not the dynamic one PMON
registers once the database is open. `db_unique_name` here is a per-host var
set in inventory (`orcl_p` on vm3, `orcl_s` on vm4), not the shared `db_name`.

### `templates/tnsnames.ora.j2`

```ini
{{ primary_db_unique_name | upper }} =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = {{ hostvars['vm3'].ansible_host }})(PORT = {{ listener_port }}))
    (CONNECT_DATA =
      (SERVER = DEDICATED)
      (SERVICE_NAME = {{ db_name }}_p)
      (UR = A)
    )
  )

{{ standby_db_unique_name | upper }} =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = {{ hostvars['vm4'].ansible_host }})(PORT = {{ listener_port }}))
    (CONNECT_DATA =
      (SERVER = DEDICATED)
      (SERVICE_NAME = {{ db_name }}_s)
      (UR = A)
    )
  )
```

`(UR = A)` ("unrestricted") lets the broker and redo transport connect
through the static listener entry even before/during a role transition,
when the normal service might be down. Both TNS aliases are rendered
identically onto **both** vm3 and vm4 — each host needs to resolve both
names, since the standby ships redo *to* itself being reachable from the
primary, and the broker on either side needs to reach the other for
`dgmgrl` commands regardless of which one is currently primary.

### `tasks/listener.yml`

```yaml
---
# Configure and start the TNS listener. Runs on both vm3 and vm4.

- name: Ensure network/admin directory exists
  file:
    path: "{{ oracle_home }}/network/admin"
    state: directory
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0755'
  tags:
    - listener

- name: Render listener.ora
  template:
    src: listener.ora.j2
    dest: "{{ oracle_home }}/network/admin/listener.ora"
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0644'
  vars:
    db_unique_name: "{{ inventory_hostname == 'vm3' | ternary(primary_db_unique_name, standby_db_unique_name) }}"
  notify: restart oracle listener
  tags:
    - listener

- name: Render tnsnames.ora
  template:
    src: tnsnames.ora.j2
    dest: "{{ oracle_home }}/network/admin/tnsnames.ora"
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0644'
  tags:
    - listener

- name: Flush handlers to apply listener config
  meta: flush_handlers
  tags:
    - listener

- name: Check listener status
  become: yes
  become_user: "{{ oracle_user }}"
  shell: "{{ oracle_home }}/bin/lsnrctl status {{ listener_name }}"
  register: lsnrctl_status
  failed_when: false
  changed_when: false
  tags:
    - listener
    - verify

- name: Start listener if not already running
  become: yes
  become_user: "{{ oracle_user }}"
  shell: "{{ oracle_home }}/bin/lsnrctl start {{ listener_name }}"
  when: "'STATUS of the LISTENER' not in lsnrctl_status.stdout"
  tags:
    - listener
```

### `handlers/main.yml`

```yaml
---
# Oracle Data Guard role handlers

- name: restart oracle listener
  become: yes
  become_user: "{{ oracle_user }}"
  shell: "{{ oracle_home }}/bin/lsnrctl restart {{ listener_name }}"
  failed_when: false
```

---
### `tasks/primary_db.yml` (vm3 only)

```yaml
---
# Create the primary database with dbca (silent), then prepare it for
# Data Guard: FORCE LOGGING, ARCHIVELOG, standby redo logs, transport params.

- name: Check whether the primary database already exists
  stat:
    path: "{{ oracle_home }}/dbs/spfile{{ oracle_sid }}.ora"
  when: inventory_hostname == 'vm3'
  register: spfile_stat
  tags:
    - primary_db

- name: Run dbca to create the primary database (silent)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dbca -silent -createDatabase \
      -templateName General_Purpose.dbc \
      -gdbname {{ db_name }} -sid {{ oracle_sid }} \
      -dbUniqueName {{ primary_db_unique_name }} \
      -createAsContainerDatabase true -numberOfPDBs 1 -pdbName {{ oracle_pdb_name }} -pdbAdminPassword '{{ oracle_pwd }}' \
      -sysPassword '{{ oracle_pwd }}' -systemPassword '{{ oracle_pwd }}' \
      -characterSet {{ character_set }} \
      -databaseType MULTIPURPOSE \
      -automaticMemoryManagement false \
      -totalMemory 2048 \
      -storageType {{ db_storage_type }} \
      -datafileDestination {{ db_data_dir }} \
      -recoveryAreaDestination {{ db_fra_dir }} -recoveryAreaSize {{ db_fra_size_mb }} \
      -enableArchive true -archiveLogMode ARCHIVELOG \
      -emConfiguration NONE \
      -ignorePreReqs \
      -silent
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_BASE: "{{ oracle_base }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: dbca_result
  when: inventory_hostname == 'vm3' and not spfile_stat.stat.exists
  tags:
    - primary_db

- name: Enable FORCE LOGGING on the primary
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    ALTER DATABASE FORCE LOGGING;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm3'
  changed_when: true
  tags:
    - primary_db
    - dataguard-prep

- name: Add standby redo log groups on the primary
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    WHENEVER SQLERROR CONTINUE
    ALTER DATABASE ADD STANDBY LOGFILE GROUP {{ item }}
      ('{{ db_data_dir }}/{{ db_name }}/stdbyredo{{ item }}.log') SIZE {{ redo_log_size_mb }}M;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  loop: "{{ range(11, 11 + standby_redo_log_count) | list }}"
  when: inventory_hostname == 'vm3'
  changed_when: true
  tags:
    - primary_db
    - dataguard-prep
```

Standby redo log groups are numbered from 11 upward specifically so they
can't collide with `dbca`'s own online redo log groups (the General_Purpose
template creates groups 1–3) — `WHENEVER SQLERROR CONTINUE` makes the loop
idempotent on rerun (`ORA-01537: group already exists` doesn't stop it).
`standby_redo_log_count` defaults to 4 — Oracle's sizing rule is "one more
group than the primary has online redo log groups," and the primary has 3
here.

```yaml
- name: Set Data Guard transport/apply parameters on the primary
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    ALTER SYSTEM SET log_archive_config='DG_CONFIG=({{ primary_db_unique_name }},{{ standby_db_unique_name }})' SCOPE=BOTH;
    ALTER SYSTEM SET log_archive_dest_2='SERVICE={{ standby_db_unique_name | upper }} ASYNC VALID_FOR=(ONLINE_LOGFILE,PRIMARY_ROLE) DB_UNIQUE_NAME={{ standby_db_unique_name }}' SCOPE=BOTH;
    ALTER SYSTEM SET log_archive_dest_state_2=ENABLE SCOPE=BOTH;
    ALTER SYSTEM SET fal_server='{{ standby_db_unique_name | upper }}' SCOPE=BOTH;
    ALTER SYSTEM SET standby_file_management=AUTO SCOPE=BOTH;
    ALTER SYSTEM SET db_create_file_dest='{{ db_data_dir }}' SCOPE=BOTH;
    ALTER SYSTEM SET dg_broker_start=TRUE SCOPE=BOTH;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm3'
  changed_when: true
  tags:
    - primary_db
    - dataguard-prep

- name: Fetch the primary's password file to the controller
  fetch:
    src: "{{ oracle_home }}/dbs/orapw{{ oracle_sid }}"
    dest: "{{ local_cert_relay_dir }}/"
    flat: yes
  when: inventory_hostname == 'vm3'
  tags:
    - primary_db
    - relay
```

`log_archive_dest_2` and `fal_server` both point at the **TNS alias**
(`ORCL_S`), not a bare hostname — that alias is what resolves through the
`tnsnames.ora` rendered by `listener.yml` earlier, the same "match the name
the other side actually expects" lesson the MSSQL build learned the hard
way with `vmware_name` vs. the inventory hostname. `dg_broker_start=TRUE`
here means the broker is live on the primary as soon as this task runs —
`dataguard_broker.yml` (below) only has to build the *configuration*, not
turn the broker process on.

The password-file fetch is the direct parallel to the MSSQL AG's
certificate relay: same `fetch` → controller → `copy` shape, just moving a
different file so the standby can authenticate as SYS over the network
before it has a database of its own.

---
### `templates/init_standby.ora.j2`

```ini
db_name={{ db_name }}
db_unique_name={{ standby_db_unique_name }}
control_files='{{ db_data_dir }}/{{ db_name }}/control01.ctl'
audit_file_dest='{{ oracle_base }}/admin/{{ oracle_sid }}/adump'
diagnostic_dest={{ oracle_base }}
```

Deliberately minimal — just enough for `startup nomount` to bring an
instance into existence so RMAN has an auxiliary to connect to. Every other
parameter (memory, redo apply config, `db_create_file_dest`, etc.) gets set
by the `DUPLICATE ... SPFILE SET ...` clause below, which replaces this
pfile with a real spfile as part of duplication.

### `tasks/standby_prep.yml` (vm4 only)

```yaml
---
# Bring up a bare "shell" instance on the standby so RMAN has something to
# duplicate into. No database exists here yet -- just enough of an instance
# for AUXILIARY connections to attach to.

- name: Check whether the standby has already been duplicated
  stat:
    path: "{{ oracle_home }}/dbs/spfile{{ oracle_sid }}.ora"
  when: inventory_hostname == 'vm4'
  register: standby_spfile_stat
  tags:
    - standby_prep

- name: Create adump directory
  file:
    path: "{{ oracle_base }}/admin/{{ oracle_sid }}/adump"
    state: directory
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0750'
    recurse: yes
  when: inventory_hostname == 'vm4' and not standby_spfile_stat.stat.exists
  tags:
    - standby_prep

- name: Create datafile directory
  file:
    path: "{{ db_data_dir }}/{{ db_name }}"
    state: directory
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0750'
    recurse: yes
  when: inventory_hostname == 'vm4' and not standby_spfile_stat.stat.exists
  tags:
    - standby_prep

- name: Render minimal standby init file
  template:
    src: init_standby.ora.j2
    dest: "{{ oracle_home }}/dbs/init{{ oracle_sid }}.ora"
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0640'
  when: inventory_hostname == 'vm4' and not standby_spfile_stat.stat.exists
  tags:
    - standby_prep

- name: Copy the primary's password file onto the standby
  copy:
    src: "{{ local_cert_relay_dir }}/orapw{{ oracle_sid }}"
    dest: "{{ oracle_home }}/dbs/orapw{{ oracle_sid }}"
    owner: "{{ oracle_user }}"
    group: "{{ oracle_group }}"
    mode: '0640'
  when: inventory_hostname == 'vm4' and not standby_spfile_stat.stat.exists
  tags:
    - standby_prep
    - relay

- name: Start the standby instance nomount
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    STARTUP NOMOUNT PFILE='{{ oracle_home }}/dbs/init{{ oracle_sid }}.ora';
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm4' and not standby_spfile_stat.stat.exists
  changed_when: true
  tags:
    - standby_prep

- name: Verify the standby instance responds over its TNS alias
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s sys/'{{ oracle_pwd }}'@{{ standby_db_unique_name | upper }} as sysdba <<'EOF'
    SELECT status FROM v\$instance;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: standby_tns_check
  when: inventory_hostname == 'vm4'
  changed_when: false
  failed_when: "'STARTED' not in standby_tns_check.stdout and 'MOUNTED' not in standby_tns_check.stdout and 'OPEN' not in standby_tns_check.stdout"
  tags:
    - standby_prep
    - verify
```

This TNS check is the real gate before attempting duplication — if the
primary can't reach `ORCL_S` over the network (firewall, listener not
running, static registration typo), `DUPLICATE ... FROM ACTIVE DATABASE`
fails with an opaque RMAN/ORA error that's harder to debug than this
sqlplus check failing cleanly first.

### `tasks/duplicate_standby.yml` (vm4 only)

```yaml
---
# Populate the standby via RMAN active database duplication -- pulls
# datafiles directly over the network from the primary, no manual backup/
# restore step and no controller relay for the database itself (unlike the
# MSSQL build's striped-backup transfer).

- name: Check whether duplication has already completed
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s sys/'{{ oracle_pwd }}'@{{ standby_db_unique_name | upper }} as sysdba <<'EOF'
    SET HEAT OFF
    SELECT database_role FROM v\$database;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: standby_role_check
  when: inventory_hostname == 'vm4'
  changed_when: false
  failed_when: false
  tags:
    - duplicate

- name: Run RMAN duplication (FROM ACTIVE DATABASE FOR STANDBY)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/rman TARGET sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} \
                    AUXILIARY sys/'{{ oracle_pwd }}'@{{ standby_db_unique_name | upper }} <<'EOF'
    DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE
    SPFILE
      SET db_unique_name='{{ standby_db_unique_name }}'
      SET fal_server='{{ primary_db_unique_name | upper }}'
      SET log_archive_dest_2='SERVICE={{ primary_db_unique_name | upper }} ASYNC VALID_FOR=(ONLINE_LOGFILE,PRIMARY_ROLE) DB_UNIQUE_NAME={{ primary_db_unique_name }}'
      SET standby_file_management='AUTO'
      SET dg_broker_start='TRUE'
      SET control_files='{{ db_data_dir }}/{{ db_name }}/control01.ctl'
    NOFILENAMECHECK;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: duplicate_result
  when: inventory_hostname == 'vm4' and "'PHYSICAL STANDBY' not in standby_role_check.stdout"
  changed_when: true
  tags:
    - duplicate

- name: Start managed recovery (redo apply) on the standby
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  when: inventory_hostname == 'vm4'
  changed_when: true
  tags:
    - duplicate
    - apply
```

`NOFILENAMECHECK` is required here specifically because primary and standby
use the **same** `db_data_dir` path on both VMs — without it RMAN refuses to
duplicate onto filenames that match the source, assuming that means you
duplicated onto the same host by mistake. `SET control_files=...` in the
`SPFILE` clause matters because the plain `db_data_dir` default would
otherwise put the standby's controlfile at whatever `db_create_file_dest`
computes, which is fine functionally but makes `validate.yml`'s path checks
unpredictable — pinning it keeps both sides symmetric.
`RECOVER MANAGED STANDBY DATABASE ... DISCONNECT` starts apply as a
background process and returns immediately rather than blocking the Ansible
task indefinitely (redo apply runs forever until a role change).

---
### `tasks/dataguard_broker.yml`

```yaml
---
# Build the Data Guard broker configuration. Runs from vm3 only -- dgmgrl
# commands against the primary propagate the configuration to both sides,
# the same way `CREATE AVAILABILITY GROUP` only runs on the MSSQL primary.

- name: Check whether a broker configuration already exists
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
    - dataguard_broker

- name: Create the broker configuration
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} <<EOF
    CREATE CONFIGURATION '{{ dg_config_name }}' AS PRIMARY DATABASE IS '{{ primary_db_unique_name }}' CONNECT IDENTIFIER IS '{{ primary_db_unique_name | upper }}';
    ADD DATABASE '{{ standby_db_unique_name }}' AS CONNECT IDENTIFIER IS '{{ standby_db_unique_name | upper }}';
    ENABLE CONFIGURATION;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  when: inventory_hostname == 'vm3' and 'ORA-16532' in dg_config_check.stdout   # ORA-16532: no broker configuration exists
  changed_when: true
  tags:
    - dataguard_broker

- name: Set the configured protection mode
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} \
      "EDIT CONFIGURATION SET PROTECTION MODE AS {{ dg_protection_mode }}"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  when: inventory_hostname == 'vm3'
  changed_when: true
  tags:
    - dataguard_broker

- name: Wait for the standby to report as a valid broker member
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} \
      "SHOW DATABASE '{{ standby_db_unique_name }}'"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: standby_broker_status
  retries: 12
  delay: 10
  until: "'SUCCESS' in standby_broker_status.stdout"
  when: inventory_hostname == 'vm3'
  changed_when: false
  tags:
    - dataguard_broker
    - verify
```

`ORA-16532` is dgmgrl's specific error for "no configuration exists yet" —
gating `CREATE CONFIGURATION` on seeing that string is this role's
`IF NOT EXISTS` equivalent, same idempotency shape as every `IF NOT EXISTS`
check in the MSSQL AG task file. Protection mode is set as its own step
after creation because `CREATE CONFIGURATION` always starts in
`MaxPerformance` regardless of what you ask for — `EDIT CONFIGURATION` is
the only way to actually change it, even when the target mode is the
default.

### `tasks/validate.yml`

```yaml
---
# Read-only verification -- run on both hosts, no writes. Mirrors the
# MSSQL build's ag_status.yml equivalent.

- name: Check instance role and open mode
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT database_role || ',' || open_mode || ',' || protection_mode FROM v\$database;
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: db_role_check
  changed_when: false
  tags:
    - validate

- name: Display instance role and open mode
  debug:
    msg: "{{ inventory_hostname }}: {{ db_role_check.stdout | trim }}"
  tags:
    - validate

- name: Check apply lag (standby only, harmless no-op on primary)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/sqlplus -s / as sysdba <<'EOF'
    SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
    SELECT name || '=' || value || unit FROM v\$dataguard_stats WHERE name IN ('apply lag','transport lag');
    EXIT;
    EOF
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
    ORACLE_SID: "{{ oracle_sid }}"
  register: lag_check
  changed_when: false
  tags:
    - validate

- name: Display lag
  debug:
    msg: "{{ inventory_hostname }}: {{ lag_check.stdout_lines }}"
  tags:
    - validate

- name: Full broker configuration status (from vm3 only, broker sees both sides)
  become: yes
  become_user: "{{ oracle_user }}"
  shell: |
    {{ oracle_home }}/bin/dgmgrl -silent sys/'{{ oracle_pwd }}'@{{ primary_db_unique_name | upper }} "SHOW CONFIGURATION VERBOSE"
  environment:
    ORACLE_HOME: "{{ oracle_home }}"
  register: broker_status
  when: inventory_hostname == 'vm3'
  changed_when: false
  tags:
    - validate

- name: Display broker configuration status
  debug:
    msg: "{{ broker_status.stdout_lines }}"
  when: inventory_hostname == 'vm3'
  tags:
    - validate

- name: Assert overall configuration status is SUCCESS
  assert:
    that:
      - "'SUCCESS' in broker_status.stdout"
    fail_msg: "Broker configuration status is not SUCCESS -- see the debug output above for details."
  when: inventory_hostname == 'vm3'
  tags:
    - validate
```

Expect `apply lag`/`transport lag` to read `0 seconds` on a healthy,
caught-up standby — a nonzero and *growing* value across repeated runs is
the Oracle-side equivalent of the MSSQL AG's `synchronization_state_desc !=
SYNCHRONIZED`, and the first thing to check if `POST /deploy/validate`
comes back unhealthy.

### `tasks/main.yml`

```yaml
---
# Main task file for embedded oracle_dg role

- name: Include host prep tasks
  include_tasks: prep.yml
  tags:
    - always

- name: Include software staging tasks
  include_tasks: stage_software.yml
  tags:
    - always

- name: Include software install tasks
  include_tasks: install_software.yml
  tags:
    - always

- name: Include listener tasks
  include_tasks: listener.yml
  tags:
    - always

- name: Include primary database tasks
  include_tasks: primary_db.yml
  tags:
    - always

- name: Include standby prep tasks
  include_tasks: standby_prep.yml
  tags:
    - always

- name: Include standby duplication tasks
  include_tasks: duplicate_standby.yml
  tags:
    - always

- name: Include Data Guard broker tasks
  include_tasks: dataguard_broker.yml
  tags:
    - always

- name: Include validation tasks
  include_tasks: validate.yml
  tags:
    - always
```

Every host-scoped file above (`primary_db.yml`, `standby_prep.yml`,
`duplicate_standby.yml`) guards its own tasks with
`when: inventory_hostname == 'vm3'` / `'vm4'` internally, the same pattern
`backup.yml`/`restore.yml` use in the MSSQL role — `main.yml` includes all
of them unconditionally on both hosts, and each task decides for itself
whether it applies. Per the note on `tags: always` under Architecture in
`CLAUDE.md` for the MSSQL build: don't rely on `--tags` filtering to skip
these `include_tasks` correctly — run whole playbooks (optionally
`--limit`), same as that project already does.

---
## Part 4 — Playbooks, inventory, and `ansible.cfg`

### `ansible/inventory/hosts.ini`

```ini
[oracle_servers]
vm3 ansible_host=192.168.70.131 vmware_name=devops_VM3 oracle_role=primary
vm4 ansible_host=192.168.70.132 vmware_name=devops_VM4 oracle_role=standby

[oracle_servers:vars]
ansible_user=devops
ansible_ssh_private_key_file=/home/devops/.ssh/id_rsa
ansible_connection=ssh
ansible_port=22
```

`vmware_name` is carried over from the MSSQL inventory's naming convention
even though this role doesn't currently need it for anything Oracle-side
(Oracle matches replicas by `db_unique_name`/TNS alias, not hostname) — kept
for consistency in case a future failover/switchover guide wants it, the
same way the MSSQL `alwayson.yml` needed `vmware_name` for `REPLICA ON`
where the AG build's earlier playbooks didn't.

### `ansible/playbooks/site.yml`

```yaml
---
- name: Provision Oracle 19c hosts and create the primary database
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  pre_tasks:
    - name: Display deployment information
      debug:
        msg: |
          Deploying Oracle 19c to {{ inventory_hostname }}
          Role: {{ oracle_role }}
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
    - name: Run host prep, software staging, install, listener, and primary DB tasks
      include_role:
        name: oracle_dg
        tasks_from: "{{ item }}"
      loop:
        - prep.yml
        - stage_software.yml
        - install_software.yml
        - listener.yml
        - primary_db.yml
      tags:
        - site

  post_tasks:
    - name: Create summary report
      copy:
        content: |
          Oracle 19c Deployment Report
          ================================
          Deployment Date: {{ ansible_date_time.iso8601 }}
          Host: {{ inventory_hostname }}
          Role: {{ oracle_role }}
          Oracle Version: {{ oracle_version }}
          Status: SUCCESS
        dest: /tmp/oracle_deployment_{{ inventory_hostname }}.txt
      tags:
        - always
```

Runs on **both** hosts — `prep.yml`/`stage_software.yml`/`install_software.yml`/
`listener.yml` all apply to both vm3 and vm4 (the standby needs a full
Oracle Home too), and `primary_db.yml` internally no-ops everywhere except
`inventory_hostname == 'vm3'`. By the end of `site.yml`: both hosts have
Oracle 19c installed and a listener running, vm3 has an open primary
database prepared for Data Guard (force logging, standby redo logs,
transport params set, password file relayed to the controller), vm4 has
nothing database-side yet.

### `ansible/playbooks/standby.yml`

```yaml
---
- name: Duplicate the standby database from the primary
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  tasks:
    - name: Show standby status
      debug:
        msg: "Processing {{ inventory_hostname }} for standby duplication"

    - name: Run standby prep and duplication tasks
      include_role:
        name: oracle_dg
        tasks_from: "{{ item }}"
      loop:
        - standby_prep.yml
        - duplicate_standby.yml
      tags:
        - standby
```

Same "runs on the group, task files gate themselves by hostname" shape as
`playbooks/backup.yml` in the MSSQL build — `standby_prep.yml` and
`duplicate_standby.yml` both no-op on vm3.

### `ansible/playbooks/dataguard.yml`

```yaml
---
- name: Configure the Data Guard broker
  hosts: oracle_servers
  become: yes
  gather_facts: yes

  pre_tasks:
    - name: Verify the listener is reachable on each host before configuring the broker
      wait_for:
        host: "{{ ansible_host }}"
        port: "{{ listener_port }}"
        timeout: 30
      tags:
        - dataguard

  tasks:
    - name: Configure Data Guard broker and validate
      include_role:
        name: oracle_dg
        tasks_from: "{{ item }}"
      loop:
        - dataguard_broker.yml
        - validate.yml
      tags:
        - dataguard
```

### `ansible/playbooks/validate.yml`

```yaml
---
- name: Read-only Data Guard status check
  hosts: oracle_servers
  become: yes
  gather_facts: no

  tasks:
    - name: Run validation tasks only
      include_role:
        name: oracle_dg
        tasks_from: validate.yml
      tags:
        - validate
```

A standalone, read-only status playbook — no writes, safe to run at any
time or on a timer. Kept separate from `dataguard.yml` on purpose: a future
failover/sync-rebuild guide (the natural next step after this one, mirroring
[`mssql-dr-failover-implementation.md`](mssql-dr-failover-implementation.md)
and
[`mssql-dr-sync-rebuild-implementation.md`](mssql-dr-sync-rebuild-implementation.md))
will want a pre/postcheck playbook exactly like this one, the same role
`ag_status.yml` plays for the MSSQL AG.

### `ansible.cfg` (in `python-fastapi-oracle-dg/`, next to `app/`)

```ini
[defaults]
log_path = ./logs/ansible.log
host_key_checking = False
```

Identical to the MSSQL service's `ansible.cfg` — same reason: persists every
run's output to `logs/ansible.log` regardless of FastAPI process restarts,
and resolves relative to wherever `uvicorn` is launched from, so always
start it from `python-fastapi-oracle-dg/`.

---
## Part 5 — The FastAPI service

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

Identical to the MSSQL service's `requirements.txt` — same FastAPI/Ansible
plumbing, nothing Oracle-specific belongs here (the Oracle client tooling
itself, `sqlplus`/`rman`/`dgmgrl`, comes from the Oracle Home installed by
Ansible on the target VMs, not from anything `pip` installs on the
controller).

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
    APP_NAME: str = "Oracle Data Guard Deployment API"
    VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"

    # Oracle configuration
    ORACLE_PWD: str = os.getenv("ORACLE_PWD", "OracleStr0ng!Passw0rd")
    ORACLE_VERSION: str = os.getenv("ORACLE_VERSION", "19.3.0.0.0")
    DB_NAME: str = os.getenv("DB_NAME", "orcl")
    LISTENER_PORT: int = int(os.getenv("LISTENER_PORT", "1521"))

    # VMware NAT target addresses
    VM3_HOST: str = os.getenv("VM3_HOST", "192.168.70.131")
    VM4_HOST: str = os.getenv("VM4_HOST", "192.168.70.132")
    VM3_USER: str = os.getenv("VM3_USER", "devops")
    VM4_USER: str = os.getenv("VM4_USER", "devops")

    # SSH configuration
    SSH_PORT: int = int(os.getenv("SSH_PORT", "22"))
    SSH_KEY_PATH: str = os.getenv("SSH_KEY_PATH", "~/.ssh/id_rsa")
    SSH_TIMEOUT: int = int(os.getenv("SSH_TIMEOUT", "30"))

    # Software staging / relay
    LOCAL_SOFTWARE_DIR: str = os.getenv("LOCAL_SOFTWARE_DIR", "./software")
    LOCAL_CERT_RELAY_DIR: str = os.getenv("LOCAL_CERT_RELAY_DIR", "./backups/oracle_dg_relay")

    # Ansible configuration
    ANSIBLE_INVENTORY: str = os.getenv("ANSIBLE_INVENTORY", "./ansible/inventory/hosts.ini")
    ANSIBLE_PLAYBOOK_DIR: str = os.getenv("ANSIBLE_PLAYBOOK_DIR", "./ansible/playbooks")
    ANSIBLE_CMD: str = os.getenv("ANSIBLE_CMD", "ansible-playbook")
    ANSIBLE_VERBOSE: int = int(os.getenv("ANSIBLE_VERBOSE", "1"))
    ANSIBLE_PRIVATE_KEY_FILE: str = os.getenv("ANSIBLE_PRIVATE_KEY_FILE", "~/.ssh/id_rsa")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: str = os.getenv("LOG_DIR", "./logs")

    # API -- Oracle installs run considerably longer than the MSSQL build
    API_TIMEOUT: int = int(os.getenv("API_TIMEOUT", "7200"))  # 2 hours

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

`API_TIMEOUT` doubles the MSSQL default — silent `runInstaller` plus `dbca`
plus an active-database RMAN duplication over the network genuinely takes
longer than the MSSQL build's AdventureWorks restore + AG join, and a
`subprocess.run(..., timeout=...)` that fires mid-`DUPLICATE` would leave
the standby instance in a half-duplicated state.

### `app/ansible_runner.py`

Identical to the MSSQL service's file — copy it as-is, nothing in it is
MSSQL-specific (it just shells out to whatever playbook name and inventory
`settings` gives it). See
[`mssql-fastapi-build-from-scratch.md` Part 5](mssql-fastapi-build-from-scratch.md#part-5--the-fastapi-service)
for the full listing; the only change worth making is swapping the class's
docstring if you want it to say "Oracle" instead of "MSSQL" — purely
cosmetic, the logic doesn't reference either product by name.

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


class AnsibleOracleDeployer:
    """Deploy Oracle 19c + Data Guard using Ansible playbooks."""

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

    def deploy_standby(self, task_id: str) -> None:
        self._run_task(task_id, lambda: self.ansible.run_playbook("standby.yml", extra_vars=self._build_extra_vars()))

    def deploy_dataguard(self, task_id: str) -> None:
        self._run_task(task_id, lambda: self.ansible.run_playbook("dataguard.yml", extra_vars=self._build_extra_vars()))

    def deploy_validate(self, task_id: str) -> None:
        self._run_task(task_id, lambda: self.ansible.run_playbook("validate.yml", extra_vars=self._build_extra_vars()))

    def deploy_full_dg(self, task_id: str) -> None:
        self._run_task(task_id, self._run_full_dg_sequence)

    def _run_full_dg_sequence(self) -> Dict[str, object]:
        results: Dict[str, object] = {}
        results["site"] = self.ansible.run_playbook("site.yml", extra_vars=self._build_extra_vars())
        if not results["site"]["success"]:
            raise SequenceStepError("site step failed; see results.site for details", results)

        results["standby"] = self.ansible.run_playbook("standby.yml", extra_vars=self._build_extra_vars())
        if not results["standby"]["success"]:
            raise SequenceStepError("standby step failed; see results.standby for details", results)

        results["dataguard"] = self.ansible.run_playbook("dataguard.yml", extra_vars=self._build_extra_vars())
        if not results["dataguard"]["success"]:
            raise SequenceStepError("dataguard step failed; see results.dataguard for details", results)

        return results

    def get_hosts(self) -> Dict:
        return {
            "hosts": [
                {"name": "vm3", "host": settings.VM3_HOST, "user": settings.VM3_USER, "port": settings.SSH_PORT, "role": "primary"},
                {"name": "vm4", "host": settings.VM4_HOST, "user": settings.VM4_USER, "port": settings.SSH_PORT, "role": "standby"},
            ]
        }

    def resolve_hosts(self) -> Dict:
        import socket

        results = {}
        for hostname in [settings.VM3_HOST, settings.VM4_HOST]:
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
        for host in [settings.VM3_HOST, settings.VM4_HOST]:
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
            "oracle_pwd": settings.ORACLE_PWD,
            "oracle_version": settings.ORACLE_VERSION,
            "db_name": settings.DB_NAME,
            "listener_port": settings.LISTENER_PORT,
            "local_software_dir": settings.LOCAL_SOFTWARE_DIR,
            "local_cert_relay_dir": settings.LOCAL_CERT_RELAY_DIR,
        }
```

Same `SequenceStepError` shape as the MSSQL deployer's `_run_full_ag_sequence`
— a failed `site` step stops before `standby` ever runs, and a failed
`standby` (duplication) stops before `dataguard` tries to broker-configure a
standby that doesn't actually exist yet. `deploy_full_dg`/`full-dg` is the
Oracle equivalent of `deploy_full_ag`/`full-ag`.

---
### `app/routes/deploy.py`

```python
"""Deployment routes"""
from fastapi import APIRouter, HTTPException, status, BackgroundTasks
import logging
from app.config import settings
from app.deployer import AnsibleOracleDeployer

router = APIRouter()
logger = logging.getLogger(__name__)

deployer = AnsibleOracleDeployer()


@router.get("/status")
async def get_deployment_status():
    """Get current deployment status"""
    history = deployer.get_history()
    latest = history[0] if history else None
    return {
        "status": latest["status"] if latest else "ready",
        "latest_task": latest,
        "engine": "ansible",
        "oracle_version": settings.ORACLE_VERSION,
        "db_name": settings.DB_NAME,
        "vm3": settings.VM3_HOST,
        "vm4": settings.VM4_HOST,
    }


@router.post("/install")
async def deploy_install(background_tasks: BackgroundTasks):
    """Provision both hosts and create the primary database.

    Long-running (30-60+ minutes depending on install media transfer speed).
    Returns immediately with a task ID.
    """
    logger.info("Received deployment request - Provision + primary DB (site.yml)")
    try:
        task_id = deployer.start_task("site")
        background_tasks.add_task(deployer.deploy_site, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Oracle provisioning + primary database creation started",
            "engine": "ansible",
            "playbook": "site.yml",
            "estimated_duration_minutes": 60,
            "instructions": "Check /api/v1/deploy/status or /api/v1/deploy/history for progress",
        }
    except Exception as e:
        logger.error(f"Error initiating deployment: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate deployment: {str(e)}")


@router.post("/standby")
async def deploy_standby(background_tasks: BackgroundTasks):
    """Duplicate the standby database from the primary via RMAN.

    Prerequisites: /install must have completed successfully first.
    """
    logger.info("Received deployment request - Standby duplication")
    try:
        task_id = deployer.start_task("standby")
        background_tasks.add_task(deployer.deploy_standby, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Standby duplication started",
            "engine": "ansible",
            "playbook": "standby.yml",
            "operations": [
                "Start a nomount shell instance on vm4",
                "Copy the primary's password file to vm4",
                "RMAN DUPLICATE ... FOR STANDBY FROM ACTIVE DATABASE",
                "Start managed recovery (redo apply)",
            ],
            "estimated_duration_minutes": 30,
        }
    except Exception as e:
        logger.error(f"Error initiating standby duplication: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate standby duplication: {str(e)}")


@router.post("/dataguard")
async def deploy_dataguard(background_tasks: BackgroundTasks):
    """Create the Data Guard broker configuration across vm3 and vm4."""
    logger.info("Received deployment request - Data Guard broker")
    try:
        task_id = deployer.start_task("dataguard")
        background_tasks.add_task(deployer.deploy_dataguard, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Data Guard broker configuration started",
            "engine": "ansible",
            "playbook": "dataguard.yml",
            "estimated_duration_minutes": 10,
            "operations": [
                "Verify listener reachability",
                "CREATE CONFIGURATION / ADD DATABASE",
                "ENABLE CONFIGURATION",
                "Set protection mode",
                "Verify broker status",
            ],
        }
    except Exception as e:
        logger.error(f"Error initiating Data Guard configuration: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate Data Guard configuration: {str(e)}")


@router.post("/full-dg")
async def deploy_full_dg(background_tasks: BackgroundTasks):
    """Run the whole build in sequence: provision + primary DB, duplicate standby, configure Data Guard."""
    logger.info("Received deployment request - Full Data Guard workflow")
    try:
        task_id = deployer.start_task("full-dg")
        background_tasks.add_task(deployer.deploy_full_dg, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Full Oracle Data Guard build started",
            "engine": "ansible",
            "playbooks": ["site.yml", "standby.yml", "dataguard.yml"],
            "operations": [
                "Provision vm3 + vm4, create primary database (vm3)",
                "Duplicate standby database from primary (vm4)",
                "Configure and enable the Data Guard broker",
            ],
            "estimated_duration_minutes": 120,
        }
    except Exception as e:
        logger.error(f"Error initiating full Data Guard workflow: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate full Data Guard workflow: {str(e)}")


@router.post("/validate")
async def deploy_validate(background_tasks: BackgroundTasks):
    """Read-only Data Guard status check -- role, open mode, apply/transport lag, broker configuration."""
    logger.info("Received deployment request - Validate (read-only)")
    try:
        task_id = deployer.start_task("validate")
        background_tasks.add_task(deployer.deploy_validate, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Validation started",
            "engine": "ansible",
            "playbook": "validate.yml",
            "estimated_duration_minutes": 2,
        }
    except Exception as e:
        logger.error(f"Error initiating validation: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate validation: {str(e)}")


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

Five write endpoints instead of the MSSQL build's three (`install`,
`backup`, `alwayson`) plus its combined `full-ag` — `standby` and `dataguard`
split out separately because RMAN duplication and broker configuration are
genuinely distinct failure domains here (a duplication that times out
midway is a very different problem than a broker `ADD DATABASE` that
fails), and because a future sync-rebuild guide will likely want to re-run
`standby` duplication on its own without re-touching the primary.

### `app/routes/health.py` and `app/routes/logs.py`

Copy both files verbatim from the MSSQL service — neither references MSSQL,
Ansible playbook names, or anything else product-specific; they operate on
generic `app.config.settings` fields (`LOG_DIR`, `SSH_KEY_PATH`, etc.) that
already exist above under the same names. See
[`mssql-fastapi-build-from-scratch.md` Part 5](mssql-fastapi-build-from-scratch.md#part-5--the-fastapi-service)
for the listing. The same caveat that guide calls out applies here too:
`health.py`'s `verify_python_ssh`/`verify_ssh_credentials` check for
Paramiko and an SSH key, not Ansible or Oracle reachability — for a real
readiness signal use `GET /api/v1/deploy/hosts` and `POST /api/v1/deploy/ping`
instead, same as the MSSQL service.

### `app/main.py`

```python
"""
Oracle Data Guard FastAPI Deployment Service
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
    title="Oracle Data Guard Deployment API",
    description="FastAPI service for Oracle 19c + Data Guard deployment automation using embedded Ansible playbooks",
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
        "service": "Oracle Data Guard Deployment API",
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
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
```

Port `8001`, not `8000` — this service runs side by side with
`python-fastapi-mssql` on the same VM1, so it defaults to a different port
rather than assuming it owns `8000`. Two separate `uvicorn` processes, two
separate `.venv`s, two separate directories — nothing shared except the
host.

### `.env`

```bash
DEBUG=False
LOG_LEVEL=INFO

ORACLE_PWD=OracleStr0ng!Passw0rd
ORACLE_VERSION=19.3.0.0.0
DB_NAME=orcl
LISTENER_PORT=1521

VM3_HOST=192.168.70.131
VM4_HOST=192.168.70.132
VM3_USER=devops
VM4_USER=devops

SSH_PORT=22
SSH_KEY_PATH=/home/devops/.ssh/id_rsa
SSH_TIMEOUT=30

ANSIBLE_INVENTORY=./ansible/inventory/hosts.ini
ANSIBLE_PLAYBOOK_DIR=./ansible/playbooks
ANSIBLE_CMD=ansible-playbook
ANSIBLE_PRIVATE_KEY_FILE=/home/devops/.ssh/id_rsa
ANSIBLE_VERBOSE=1

LOCAL_SOFTWARE_DIR=./software
LOCAL_CERT_RELAY_DIR=./backups/oracle_dg_relay

API_TIMEOUT=7200
```

Change `ORACLE_PWD` before any real use — it's the SYS/SYSTEM/PDB-admin
password *and* the Data Guard broker connect password in this design, so
treat it with the same care as the MSSQL build's `MSSQL_SA_PASSWORD`.

---
## Part 6 — Install and run

```bash
cd python-fastapi-oracle-dg
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# put the Oracle install zip in place before anything else
ls software/LINUX.X64_193000_db_home.zip   # should exist -- see Prerequisites

uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

Confirm it's up:
```bash
curl http://localhost:8001/api/v1/health/check
```

## Part 7 — Build it: the CLI way

**1. Confirm both VMs are reachable:**
```bash
curl -X POST http://localhost:8001/api/v1/deploy/ping
curl http://localhost:8001/api/v1/deploy/hosts | jq
```

**2. Run the whole build in one call** — provisions both hosts, creates the
primary database, duplicates the standby, and configures the Data Guard
broker:
```bash
curl -X POST http://localhost:8001/api/v1/deploy/full-dg
```
Budget 2+ hours end to end (software transfer + silent install on two hosts
+ `dbca` + active-database duplication over the network). Watch it live:
```bash
tail -f logs/ansible.log
```

**3. Or run it step by step**, if you want to see each stage individually —
useful the first time through, since a failure in `dbca` or `runInstaller`
is much easier to diagnose from a single playbook's output than buried in a
2-hour combined run:
```bash
curl -X POST http://localhost:8001/api/v1/deploy/install     # site.yml: both VMs provisioned, primary DB created
curl -X POST http://localhost:8001/api/v1/deploy/standby      # standby.yml: RMAN duplication onto vm4
curl -X POST http://localhost:8001/api/v1/deploy/dataguard    # dataguard.yml: broker configuration
```

**4. Check progress/result at any point:**
```bash
curl http://localhost:8001/api/v1/deploy/status | jq
curl http://localhost:8001/api/v1/deploy/history | jq '.executions[0]'
```

**5. Verify independently** — don't just trust the playbook's own `debug`
output:
```bash
# from vm3, as the oracle user
dgmgrl sys/'<oracle_pwd>'@ORCL_P "SHOW CONFIGURATION VERBOSE"
sqlplus -s / as sysdba <<'EOF'
SELECT database_role, open_mode, protection_mode FROM v$database;
EOF
```
Expect `PRIMARY` / `READ WRITE` on vm3, `PHYSICAL STANDBY` / `MOUNTED` on
vm4, and the broker configuration reporting `SUCCESS`.

## Part 8 — Build it: the Swagger UI way

1. Open `http://<vm1-ip>:8001/api/docs` in a browser (or
   `http://localhost:8001/api/docs` if you're on VM1 itself).
2. Expand **deployment → POST /api/v1/deploy/ping**, **Try it out**,
   **Execute**. Confirm both hosts come back `"reachable"`.
3. Expand **POST /api/v1/deploy/full-dg**, **Try it out**, **Execute** with
   an empty body. The response shows `"status": "initiated"` and a
   `task_id` immediately — the Ansible run continues in the background.
4. Poll the same way as the MSSQL build: **GET /api/v1/deploy/status** or
   **/history**, **Execute** again periodically. `status` moves
   `"running"` → `"success"` or `"failed"` (with `error` and whatever
   partial `results` `SequenceStepError` captured).
5. **health** and **logs** are safe to click any time (all read-only
   `GET`s except `POST /logs/clear`).

## Part 9 — Rerun it

**If both VMs are still bare**, rerunning is identical to Part 7/8:
`POST /deploy/full-dg` again.

**If Data Guard is already built and healthy**, calling `full-dg` again will
**not** cleanly rebuild it: `primary_db.yml`'s `dbca -createDatabase` step is
guarded by checking for an existing spfile, so it skips rather than erroring
— but `standby.yml`'s duplication step checks `v$database.database_role`
for `PHYSICAL STANDBY` and skips too, meaning `full-dg` on an already-healthy
pair is actually idempotent end to end and mostly a no-op, unlike the MSSQL
build's `full-ag` (whose `RESTORE DATABASE ... WITH REPLACE` genuinely
conflicts with an existing AG). The one exception: `dataguard_broker.yml`'s
`CREATE CONFIGURATION` is also gated (on `ORA-16532`), so a rerun there is
safe too.

Tearing down an Oracle Data Guard pair cleanly (remove the broker
configuration, stop recovery, drop the standby, `dbca -deleteDatabase`,
deinstall software) is its own topic — covered in
[`oracle-rewind-and-teardown-implementation.md`](oracle-rewind-and-teardown-implementation.md),
the same way
[`mssql-rewind-and-teardown-implementation.md`](mssql-rewind-and-teardown-implementation.md)
followed the MSSQL AG build guide rather than being part of it. That doc
adds `POST /deploy/teardown`, `/rewind`, and `/reset-baseline` — once
they're in place, the clean-rerun sequence is:
```bash
curl -X POST http://localhost:8001/api/v1/deploy/reset-baseline   # wipe both VMs
curl -X POST http://localhost:8001/api/v1/deploy/full-dg           # rebuild from bare
```

---

## Open questions to confirm before you run this

Because nothing here has been tested against VM3/VM4 yet, these are the
places most likely to need a tweak once you do:

1. **OS family/version.** This guide assumes RHEL-compatible 8.x (matching
   VM1/VM2). If VM3/VM4 are a different distro or major version, the
   package list in `prep.yml` and the `assert` in `site.yml` need updating
   first — everything downstream depends on `yum`/`firewalld` working as
   written.
2. **Oracle edition/patch level.** This targets base 19.3 Enterprise
   Edition with no Release Update applied. If you have a newer RU zip
   instead of (or in addition to) the base 19.3 media, `oracle_zip_name`
   and the `stage_software.yml`/`install_software.yml` pair need an extra
   "apply RU via `datapatch`" step this guide doesn't include.
3. **Storage layout.** `db_storage_type: FS` (plain filesystem) was chosen
   to match the design doc's "keep it simple" framing. If you'd rather use
   ASM (closer to a real production DG setup), most of `primary_db.yml`
   and `duplicate_standby.yml` change shape — that's a meaningfully bigger
   rewrite, not a variable flip.
4. **Protection mode.** `MaxPerformance`/`ASYNC` was chosen because a lab
   with one network path between two VMs has no redundancy to justify
   `MaxAvailability`'s `SYNC` transport, which can stall primary commits if
   the standby lags. Easy to change (`dg_protection_mode` var) if you want
   to experiment with the stricter modes once the basic pair is healthy.
5. **Memory sizing.** `-totalMemory 2048` (2 GB SGA+PGA) in the `dbca`
   command is deliberately conservative for a lab VM — bump it in
   `primary_db.yml` if VM3/VM4 have more than the 8 GB minimum this guide
   assumes.

---
