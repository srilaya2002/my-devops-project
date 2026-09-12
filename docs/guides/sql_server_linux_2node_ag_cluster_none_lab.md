# SQL Server Always On Availability Group (AG) on Linux — 2-Node `CLUSTER_TYPE = NONE` Lab

## Overview

This document captures the SQL Server Availability Group (AG) setup discussed in this chat.

The lab is deliberately simple:

- 2 Linux VMs
- SQL Server running on each VM
- SQL Server Always On Availability Group
- `CLUSTER_TYPE = NONE`
- **No Pacemaker**
- **No Corosync**
- **No Linux cluster**
- Manual failover
- Optional AG listener
- VS Code + Microsoft MSSQL extension on an Intel MacBook Pro as the SQL client

The purpose is to learn the SQL Server AG concepts without introducing Linux cluster management at the same time.

---

# 1. Overall Architecture

```text
                 INTEL MACBOOK PRO
                 ┌─────────────────────┐
                 │       macOS         │
                 │                     │
                 │      VS Code        │
                 │        │            │
                 │        ▼            │
                 │  MSSQL Extension   │
                 └─────────┬───────────┘
                           │
                      SQL connection
                           │
                           ▼
                  ┌─────────────────┐
                  │   AG Listener   │
                  │ aglistener:1433 │
                  └────────┬────────┘
                           │
                  ┌────────┴────────┐
                  │                 │
                  ▼                 ▼
           ┌─────────────┐   ┌─────────────┐
           │   Linux VM1 │   │   Linux VM2 │
           │             │   │             │
           │ SQL Server  │   │ SQL Server  │
           │  PRIMARY    │──▶│  SECONDARY  │
           │             │   │             │
           └─────────────┘   └─────────────┘
                  │                 │
                  └──── AG/HADR ────┘
                       TCP 5022
```

## Important distinction

The terms `PRIMARY` and `SECONDARY` are SQL Server AG roles.

If VM2 is being used as your "test" machine, that does **not** mean SQL Server has a `TEST` AG role.

```text
VM1 = PRIMARY
VM2 = SECONDARY

VM2 may be called "TEST" by you,
but its SQL Server AG role is SECONDARY.
```

---

# 2. What `CLUSTER_TYPE = NONE` Means

This lab uses:

```text
CLUSTER_TYPE = NONE
```

There is no cluster manager underneath the AG.

```text
             SQL Server AG
              CLUSTER_TYPE
                   = NONE
                     │
          ┌──────────┴──────────┐
          │                     │
          ▼                     ▼
       Linux VM1             Linux VM2
       PRIMARY               SECONDARY
          │                     │
          └────── AG ───────────┘
               TCP 5022
```

Not involved:

```text
Pacemaker       ❌
Corosync        ❌
Linux cluster   ❌
WSFC            ❌
```

The focus is therefore on SQL Server AG itself:

```text
AG
│
├── PRIMARY / SECONDARY
├── Replication
├── Synchronous / Asynchronous commit
├── HADR endpoint
├── Synchronization
├── Failover
├── Forced failover
└── Application connections
```

For a learning lab, manual failover keeps the concepts easier to understand.

---

# 3. Suggested Simple Configuration

```text
AG name:
    AG1

VM1:
    SQL Server
    PRIMARY
    Synchronous commit
    Manual failover

VM2:
    SQL Server
    SECONDARY
    Synchronous commit
    Manual failover

HADR endpoint:
    TCP 5022

SQL Server:
    TCP 1433

Listener:
    aglistener:1433
```

Conceptually:

```text
                 AG1
                  │
          ┌───────┴────────┐
          │                │
          ▼                ▼
        VM1              VM2
      PRIMARY          SECONDARY
          │                │
          └──── AG ────────┘
             TCP 5022
```

---

# 4. VS Code on the Intel MacBook Pro

For the SQL client, use:

```text
VS Code
+
Microsoft MSSQL extension
```

The MSSQL extension is Microsoft's recommended direction after Azure Data Studio retirement.

You do **not** need to install VS Code inside the Linux VM just to connect to SQL Server.

Your environment can be:

```text
MacBook Pro
│
├── VS Code
│     │
│     └── MSSQL extension
│
└── VMware Fusion
      │
      ├── Linux VM1
      │     └── SQL Server
      │
      └── Linux VM2
            └── SQL Server
```

In VS Code, open Extensions with:

```text
Cmd + Shift + X
```

Search for:

```text
SQL Server (mssql)
```

---

# 5. SQL Server Client Connection

A normal SQL Server connection uses port:

```text
1433
```

The AG replica communication endpoint is typically:

```text
5022
```

Therefore:

```text
1433 = SQL Server client connections

5022 = AG/HADR replica communication
```

If using an AG listener, the application/client should connect to:

```text
aglistener,1433
```

rather than directly connecting to:

```text
VM1,1433
```

or:

```text
VM2,1433
```

---

# 6. Query 1 — Identify the SQL Server Instance

Run on both VMs:

```sql
SELECT
    @@SERVERNAME AS server_name,
    SERVERPROPERTY('MachineName') AS machine_name,
    SERVERPROPERTY('ServerName') AS sql_server_name,
    SERVERPROPERTY('Edition') AS edition,
    SERVERPROPERTY('ProductVersion') AS product_version;
```

This tells you which SQL Server instance you are connected to.

---

# 7. Query 2 — Identify the Current Connection

```sql
SELECT
    HOST_NAME() AS client_host,
    SUSER_SNAME() AS login_name,
    DB_NAME() AS database_name,
    GETDATE() AS current_time;
```

This is particularly useful when testing from VS Code.

---

# 8. Query 3 — Check Whether Always On Is Enabled

```sql
SELECT
    SERVERPROPERTY('IsHadrEnabled') AS is_hadr_enabled;
```

Expected:

```text
is_hadr_enabled
---------------
1
```

If it returns:

```text
0
```

Always On is not enabled on that SQL Server instance.

---

# 9. Query 4 — Show the AGs Configured on the Server

```sql
SELECT
    name AS ag_name,
    group_id,
    resource_id,
    failure_condition_level,
    health_check_timeout
FROM sys.availability_groups;
```

Example:

```text
ag_name
-------
AG1
```

---

# 10. Query 5 — Check `CLUSTER_TYPE`

This is the key query for this particular lab.

```sql
SELECT
    name AS ag_name,
    cluster_type_desc,
    automated_backup_preference_desc,
    failure_condition_level,
    health_check_timeout,
    db_failover
FROM sys.availability_groups;
```

For this lab you want:

```text
ag_name   cluster_type_desc
-------   -----------------
AG1       NONE
```

The important value is:

```text
CLUSTER_TYPE = NONE
```

---

# 11. Query 6 — Find PRIMARY vs SECONDARY

This is one of the most important AG queries.

```sql
SELECT
    ag.name AS ag_name,
    ar.replica_server_name,
    ars.role_desc,
    ars.connected_state_desc,
    ars.synchronization_health_desc
FROM sys.availability_groups AS ag
JOIN sys.availability_replicas AS ar
    ON ag.group_id = ar.group_id
JOIN sys.dm_hadr_availability_replica_states AS ars
    ON ar.group_id = ars.group_id
    AND ar.replica_id = ars.replica_id;
```

Example:

```text
AG1   VM1   PRIMARY     CONNECTED   HEALTHY
AG1   VM2   SECONDARY   CONNECTED   HEALTHY
```

---

# 12. Query 7 — Find the Role of the Current Server

This is the simplest query to determine whether the SQL Server you are connected to is PRIMARY or SECONDARY.

```sql
SELECT
    @@SERVERNAME AS server_name,
    ag.name AS ag_name,
    ars.role_desc
FROM sys.availability_groups AS ag
JOIN sys.dm_hadr_availability_replica_states AS ars
    ON ag.group_id = ars.group_id
WHERE ars.is_local = 1;
```

On VM1:

```text
VM1    AG1    PRIMARY
```

On VM2:

```text
VM2    AG1    SECONDARY
```

This is a good query to save as:

```text
check_ag_role.sql
```

---

# 13. Query 8 — Show Both Replicas

```sql
SELECT
    ag.name AS ag_name,
    ar.replica_server_name,
    ars.role_desc,
    ars.operational_state_desc,
    ars.connected_state_desc,
    ars.recovery_health_desc,
    ars.synchronization_health_desc
FROM sys.availability_groups ag
JOIN sys.availability_replicas ar
    ON ag.group_id = ar.group_id
JOIN sys.dm_hadr_availability_replica_states ars
    ON ar.group_id = ars.group_id
    AND ar.replica_id = ars.replica_id
ORDER BY
    ag.name,
    ar.replica_server_name;
```

Healthy example:

```text
AG1
├── VM1
│   ├── PRIMARY
│   ├── ONLINE
│   ├── CONNECTED
│   └── HEALTHY
│
└── VM2
    ├── SECONDARY
    ├── ONLINE
    ├── CONNECTED
    └── HEALTHY
```

---

# 14. Query 9 — Show Complete Replica Configuration

```sql
SELECT
    ag.name AS ag_name,
    ag.cluster_type_desc,
    ar.replica_server_name,
    ars.role_desc,
    ars.connected_state_desc,
    ars.synchronization_health_desc,
    ar.availability_mode_desc,
    ar.failover_mode_desc,
    ar.endpoint_url,
    ar.seeding_mode_desc
FROM sys.availability_groups AS ag
JOIN sys.availability_replicas AS ar
    ON ag.group_id = ar.group_id
JOIN sys.dm_hadr_availability_replica_states AS ars
    ON ar.group_id = ars.group_id
    AND ar.replica_id = ars.replica_id
ORDER BY
    ar.replica_server_name;
```

Example conceptual output:

```text
AG1
│
├── VM1
│   ├── PRIMARY
│   ├── CONNECTED
│   ├── HEALTHY
│   ├── SYNCHRONOUS_COMMIT
│   ├── MANUAL
│   └── TCP://VM1:5022
│
└── VM2
    ├── SECONDARY
    ├── CONNECTED
    ├── HEALTHY
    ├── SYNCHRONOUS_COMMIT
    ├── MANUAL
    └── TCP://VM2:5022
```

---

# 15. Query 10 — Show Replica Configuration

```sql
SELECT
    replica_server_name,
    availability_mode_desc,
    failover_mode_desc,
    session_timeout,
    endpoint_url,
    primary_role_allow_connections_desc,
    secondary_role_allow_connections_desc,
    seeding_mode_desc
FROM sys.availability_replicas;
```

For the simple lab you may see:

```text
VM1   SYNCHRONOUS_COMMIT   MANUAL
VM2   SYNCHRONOUS_COMMIT   MANUAL
```

---

# 16. Query 11 — Check the HADR Endpoint

AG replicas communicate through the database mirroring/HADR endpoint.

```sql
SELECT
    name,
    state_desc,
    type_desc,
    port
FROM sys.database_mirroring_endpoints;
```

Expected conceptually:

```text
name     state       type
--------------------------------
Hadr     STARTED     DATABASE_MIRRORING
```

---

# 17. Query 12 — Check Replica Endpoint URLs

```sql
SELECT
    replica_server_name,
    endpoint_url
FROM sys.availability_replicas;
```

Example:

```text
VM1   TCP://VM1:5022
VM2   TCP://VM2:5022
```

Remember:

```text
1433 = SQL Server client port

5022 = AG/HADR endpoint
```

---

# 18. Query 13 — Check AG Connection State

```sql
SELECT
    ar.replica_server_name,
    ars.connected_state_desc,
    ars.last_connect_error_number,
    ars.last_connect_error_description,
    ars.last_connect_error_timestamp
FROM sys.availability_replicas AS ar
JOIN sys.dm_hadr_availability_replica_states AS ars
    ON ar.replica_id = ars.replica_id;
```

Healthy example:

```text
VM1   CONNECTED
VM2   CONNECTED
```

If the replicas cannot communicate, the error fields can help diagnose the problem.

---

# 19. Query 14 — Check Replica Health

```sql
SELECT
    ar.replica_server_name,
    ars.role_desc,
    ars.operational_state_desc,
    ars.connected_state_desc,
    ars.recovery_health_desc,
    ars.synchronization_health_desc
FROM sys.availability_replicas ar
JOIN sys.dm_hadr_availability_replica_states ars
    ON ar.replica_id = ars.replica_id;
```

Ideally:

```text
PRIMARY
ONLINE
CONNECTED
ONLINE
HEALTHY
```

and:

```text
SECONDARY
ONLINE
CONNECTED
ONLINE
HEALTHY
```

---

# 20. Query 15 — Check AG Databases

```sql
SELECT
    ag.name AS ag_name,
    adc.database_name
FROM sys.availability_groups ag
JOIN sys.availability_databases_cluster adc
    ON ag.group_id = adc.group_id;
```

Example:

```text
AG1    SalesDB
AG1    TestDB
```

---

# 21. Query 16 — Check Database Synchronization

```sql
SELECT
    DB_NAME(database_id) AS database_name,
    synchronization_state_desc,
    synchronization_health_desc
FROM sys.dm_hadr_database_replica_states
WHERE is_local = 1;
```

For a synchronous-commit lab you want:

```text
SYNCHRONIZED
HEALTHY
```

---

# 22. Query 17 — Detailed Database Synchronization

```sql
SELECT
    DB_NAME(drs.database_id) AS database_name,
    ar.replica_server_name,
    ars.role_desc,
    drs.synchronization_state_desc,
    drs.synchronization_health_desc,
    drs.database_state_desc
FROM sys.dm_hadr_database_replica_states drs
JOIN sys.availability_replicas ar
    ON drs.replica_id = ar.replica_id
JOIN sys.dm_hadr_availability_replica_states ars
    ON ar.replica_id = ars.replica_id
WHERE drs.group_id IS NOT NULL
ORDER BY
    database_name,
    ar.replica_server_name;
```

Example:

```text
Database     Server       Role       Sync          Health
------------------------------------------------------------
SalesDB      VM1          PRIMARY    SYNCHRONIZED  HEALTHY
SalesDB      VM2          SECONDARY  SYNCHRONIZED  HEALTHY
```

---

# 23. Query 18 — Show AG Database State

```sql
SELECT
    DB_NAME(drs.database_id) AS database_name,
    drs.is_local,
    drs.is_primary_replica,
    drs.synchronization_state_desc,
    drs.synchronization_health_desc
FROM sys.dm_hadr_database_replica_states drs;
```

---

# 24. Query 19 — Show PRIMARY Databases on the Current Server

```sql
SELECT
    DB_NAME(database_id) AS database_name,
    synchronization_state_desc,
    synchronization_health_desc
FROM sys.dm_hadr_database_replica_states
WHERE is_local = 1
  AND is_primary_replica = 1;
```

---

# 25. Query 20 — Show SECONDARY Databases on the Current Server

```sql
SELECT
    DB_NAME(database_id) AS database_name,
    synchronization_state_desc,
    synchronization_health_desc
FROM sys.dm_hadr_database_replica_states
WHERE is_local = 1
  AND is_primary_replica = 0;
```

---

# 26. Query 21 — Check Log Send and Redo Queues

This is useful when deliberately generating load or testing whether the secondary is falling behind.

```sql
SELECT
    DB_NAME(drs.database_id) AS database_name,
    ar.replica_server_name,
    drs.synchronization_state_desc,
    drs.synchronization_health_desc,
    drs.log_send_queue_size,
    drs.redo_queue_size,
    drs.log_send_rate,
    drs.redo_rate
FROM sys.dm_hadr_database_replica_states AS drs
JOIN sys.availability_replicas AS ar
    ON drs.replica_id = ar.replica_id
ORDER BY
    database_name,
    ar.replica_server_name;
```

Conceptually:

```text
PRIMARY
   |
   | transaction log
   ▼
SECONDARY
   |
   ├── log send queue
   └── redo queue
```

If these queues grow significantly, the secondary may be falling behind.

---

# 27. Query 22 — Check LSN/Queue Information

```sql
SELECT
    DB_NAME(drs.database_id) AS database_name,
    ar.replica_server_name,
    drs.last_hardened_lsn,
    drs.last_redone_lsn,
    drs.log_send_queue_size,
    drs.redo_queue_size
FROM sys.dm_hadr_database_replica_states drs
JOIN sys.availability_replicas ar
    ON drs.replica_id = ar.replica_id;
```

This can be useful when investigating synchronization behaviour.

---

# 28. Query 23 — Check AG Listener

If your AG has a listener:

```sql
SELECT
    ag.name AS ag_name,
    agl.dns_name,
    agl.port_number,
    agl.ip_configuration_string_from_cluster
FROM sys.availability_group_listeners agl
JOIN sys.availability_groups ag
    ON ag.group_id = agl.group_id;
```

Example:

```text
AG1
aglistener
1433
```

Clients can then connect using:

```text
aglistener,1433
```

rather than directly connecting to a specific replica.

---

# 29. Query 24 — Detailed Listener Information

```sql
SELECT
    ag.name AS ag_name,
    agl.dns_name,
    agl.port_number,
    ip.ip_address,
    ip.subnet_mask
FROM sys.availability_group_listeners agl
JOIN sys.availability_groups ag
    ON ag.group_id = agl.group_id
JOIN sys.availability_group_listener_ip_addresses ip
    ON agl.listener_id = ip.listener_id;
```

---

# 30. Query 25 — All-in-One AG Status Query

This is a useful query to save as:

```text
ag_status.sql
```

```sql
SELECT
    ag.name AS ag_name,
    ag.cluster_type_desc,
    ar.replica_server_name,
    ars.role_desc,
    ars.operational_state_desc,
    ars.connected_state_desc,
    ars.recovery_health_desc,
    ars.synchronization_health_desc,
    ar.availability_mode_desc,
    ar.failover_mode_desc,
    ar.endpoint_url
FROM sys.availability_groups AS ag
JOIN sys.availability_replicas AS ar
    ON ag.group_id = ar.group_id
JOIN sys.dm_hadr_availability_replica_states AS ars
    ON ar.replica_id = ars.replica_id
    AND ar.group_id = ars.group_id
ORDER BY
    ag.name,
    ar.replica_server_name;
```

Conceptual output:

```text
AG1
│
├── VM1
│   ├── PRIMARY
│   ├── CONNECTED
│   ├── HEALTHY
│   ├── SYNCHRONOUS_COMMIT
│   └── MANUAL
│
└── VM2
    ├── SECONDARY
    ├── CONNECTED
    ├── HEALTHY
    ├── SYNCHRONOUS_COMMIT
    └── MANUAL
```

---

# 31. Quick Diagnostic Cheat Sheet

## Which server am I on?

```sql
SELECT @@SERVERNAME;
```

## Is Always On enabled?

```sql
SELECT SERVERPROPERTY('IsHadrEnabled');
```

## Is this AG `CLUSTER_TYPE = NONE`?

```sql
SELECT
    name,
    cluster_type_desc
FROM sys.availability_groups;
```

## Am I PRIMARY or SECONDARY?

```sql
SELECT
    @@SERVERNAME AS server_name,
    ag.name AS ag_name,
    ars.role_desc
FROM sys.availability_groups ag
JOIN sys.dm_hadr_availability_replica_states ars
    ON ag.group_id = ars.group_id
WHERE ars.is_local = 1;
```

## What are both replicas doing?

```sql
SELECT
    ag.name AS ag_name,
    ar.replica_server_name,
    ars.role_desc,
    ars.connected_state_desc,
    ars.synchronization_health_desc
FROM sys.availability_groups ag
JOIN sys.availability_replicas ar
    ON ag.group_id = ar.group_id
JOIN sys.dm_hadr_availability_replica_states ars
    ON ar.group_id = ars.group_id
    AND ar.replica_id = ars.replica_id;
```

## Are databases synchronized?

```sql
SELECT
    DB_NAME(database_id) AS database_name,
    synchronization_state_desc,
    synchronization_health_desc
FROM sys.dm_hadr_database_replica_states
WHERE is_local = 1;
```

## Is the HADR endpoint running?

```sql
SELECT
    name,
    state_desc,
    type_desc,
    port
FROM sys.database_mirroring_endpoints;
```

## Are replicas connected?

```sql
SELECT
    ar.replica_server_name,
    ars.connected_state_desc,
    ars.last_connect_error_number,
    ars.last_connect_error_description,
    ars.last_connect_error_timestamp
FROM sys.availability_replicas ar
JOIN sys.dm_hadr_availability_replica_states ars
    ON ar.replica_id = ars.replica_id;
```

---

# 32. Suggested Learning Exercise

The best way to learn this setup is to deliberately change state and observe what the queries report.

```text
01. VM1 = PRIMARY
        │
        ▼
02. Insert test data
        │
        ▼
03. VM2 receives the data
        │
        ▼
04. Check SYNCHRONIZED
        │
        ▼
05. Perform manual failover
        │
        ▼
06. VM2 becomes PRIMARY
        │
        ▼
07. VM1 becomes SECONDARY
        │
        ▼
08. Connect through the listener
        │
        ▼
09. Insert more test data
        │
        ▼
10. Verify the data on the other replica
        │
        ▼
11. Fail back to VM1
```

---

# 33. Mental Model

Keep these layers separate:

```text
                    CLIENT
                      │
                      │ TCP 1433
                      ▼
               AG LISTENER
                      │
          ┌───────────┴───────────┐
          │                       │
          ▼                       ▼
       SQL VM1                 SQL VM2
       PRIMARY                 SECONDARY
          │                       │
          └──── HADR/AG ──────────┘
                 TCP 5022
```

The important concepts are:

```text
SQL Server
   │
   ├── Instance
   │
   ├── Database
   │
   └── Availability Group
          │
          ├── Replica 1 → PRIMARY
          │
          └── Replica 2 → SECONDARY
```

And in this lab:

```text
Availability Group
        │
        └── CLUSTER_TYPE = NONE
```

Therefore there is no Pacemaker or Corosync layer.

---

# 34. Recommended First Queries to Run

When troubleshooting the lab, run these in this order:

### Step 1 — Identify the server

```sql
SELECT
    @@SERVERNAME AS server_name,
    SERVERPROPERTY('MachineName') AS machine_name;
```

### Step 2 — Check Always On

```sql
SELECT
    SERVERPROPERTY('IsHadrEnabled') AS is_hadr_enabled;
```

### Step 3 — Check cluster type

```sql
SELECT
    name AS ag_name,
    cluster_type_desc
FROM sys.availability_groups;
```

Expected:

```text
AG1    NONE
```

### Step 4 — Check local AG role

```sql
SELECT
    @@SERVERNAME AS server_name,
    ag.name AS ag_name,
    ars.role_desc
FROM sys.availability_groups ag
JOIN sys.dm_hadr_availability_replica_states ars
    ON ag.group_id = ars.group_id
WHERE ars.is_local = 1;
```

### Step 5 — Check both replicas

```sql
SELECT
    ag.name AS ag_name,
    ar.replica_server_name,
    ars.role_desc,
    ars.connected_state_desc,
    ars.synchronization_health_desc
FROM sys.availability_groups ag
JOIN sys.availability_replicas ar
    ON ag.group_id = ar.group_id
JOIN sys.dm_hadr_availability_replica_states ars
    ON ar.group_id = ars.group_id
    AND ar.replica_id = ars.replica_id;
```

### Step 6 — Check database synchronization

```sql
SELECT
    DB_NAME(database_id) AS database_name,
    synchronization_state_desc,
    synchronization_health_desc
FROM sys.dm_hadr_database_replica_states
WHERE is_local = 1;
```

### Step 7 — Check the HADR endpoint

```sql
SELECT
    name,
    state_desc,
    type_desc,
    port
FROM sys.database_mirroring_endpoints;
```

---

# 35. Final Picture

Your intended environment is essentially:

```text
                         INTEL MACBOOK PRO
                              macOS
                               │
                               │
                         ┌─────▼─────┐
                         │  VS Code  │
                         │  MSSQL    │
                         │ Extension │
                         └─────┬─────┘
                               │
                         SQL connection
                               │
                               ▼
                       ┌────────────────┐
                       │ AG Listener    │
                       │ aglistener:1433│
                       └───────┬────────┘
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
                 ▼                           ▼
        ┌─────────────────┐         ┌─────────────────┐
        │    VMware VM1   │         │    VMware VM2   │
        │                 │         │                 │
        │ Linux           │         │ Linux           │
        │ SQL Server      │         │ SQL Server      │
        │                 │         │                 │
        │    PRIMARY      │◀───────▶│   SECONDARY     │
        │                 │  AG     │                 │
        └─────────────────┘ TCP5022 └─────────────────┘
                 │                           │
                 └───────────────────────────┘

                    CLUSTER_TYPE = NONE

                    Pacemaker  ❌
                    Corosync   ❌
                    WSFC       ❌
                    Linux      ❌
                    cluster

                    Manual failover
```

The key idea is:

> **This is SQL Server Availability Groups without a cluster manager.**

The two SQL Server instances communicate directly through their HADR endpoints, and you use the SQL Server AG DMVs and catalog views above to determine roles, health, synchronization, endpoints, queues, and listener configuration.
