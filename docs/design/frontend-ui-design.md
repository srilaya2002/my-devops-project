# Frontend UI Design — DevOps Console (MSSQL / Oracle / MySQL)

## 1. Overview

**Goal:** a single React + Tailwind, dark-mode-first web console that lets a lab operator trigger
and monitor the four lifecycle phases (Build AG, Rewind/Teardown, DR Failover, DR Sync) for each of
the three database platforms this repo automates: MSSQL, Oracle, MySQL.

**Status of this document:** initial UI design only — no code. This is the artifact to review and
iterate on before any React project is scaffolded.

**Backend reality today:**

- **MSSQL is live.** `python-fastapi-mssql/app/routes/deploy.py` (prefix `/api/v1/deploy`) is a
  real, working FastAPI service verified end-to-end against `devops_VM1` / `devops_VM2`.
- **Oracle and MySQL are design-only.** `docs/guides/oracle-*` (VM3=`devops_VM3` primary,
  VM4=`devops_VM4` standby, Data Guard) and `docs/guides/mysql-*` (vm5 primary, vm6 replica,
  XtraBackup + GTID replication) describe a FastAPI service mirroring the same endpoint shape, but
  no `python-fastapi-oracle-dg/` or `python-fastapi-mysql/` code exists yet.

Because of this, the UI is designed **RDBMS-agnostic**: one page template driven by a small
per-database config object, so Oracle and MySQL are drop-in once their backends exist — today they
render as "coming soon" using the same layout, which also doubles as a stakeholder preview.

**Async execution model (drives most of the UX):** every MSSQL `POST` action returns a `task_id`
immediately and runs the underlying Ansible playbook in the background — there is no synchronous
request/response. Every phase page therefore needs the same three-part pattern: **trigger a job →
poll its status → tail its log** — not a plain form submit.

**Design correction from v1:** the first pass of this doc rendered each endpoint as a button with
its path printed next to it — effectively a styled Swagger index. That's not the target. The React
app calls these FastAPI endpoints directly (no separate API-docs step for the operator), so every
endpoint gets a real **request panel** (its actual input fields, or an explicit "no parameters"
state) and a real **response panel** (the actual JSON envelope the route returns, rendered as
labeled fields plus a raw-JSON toggle) — not just a label. The field names below are taken directly
from `python-fastapi-mssql/app/routes/deploy.py` and `app/deployer.py`, not invented:

- Every `POST` trigger returns the same envelope shape:
  `{status: "initiated", task_id, message, engine: "ansible", playbook | playbooks, estimated_duration_minutes, ...operations/tags/notes}`.
- `GET /status` → `{status, latest_task, engine, mssql_version, mssql_edition, vm1, vm2}`.
- `GET /history` → `{total_executions, executions: [{task_id, operation, status, started_at, completed_at, results, error}]}`,
  where a completed entry's `results` is `{playbook, command, return_code, stdout, stderr, started_at, completed_at, duration_seconds, success}`.
- `GET /hosts` → `{status, inventory: {hosts: [{name, host, user, port}]}, dns: {<hostname>: {address, resolved}}, ansible_inventory, ansible_playbooks}`.
- `POST /ping` → `{status, results: [{host, status, error?}]}`.
- `GET /rewind-plan` → static plan doc: `{note, teardown: {...}, rewind: {...}, "reset-baseline": {...}}`, each with `playbook`, `endpoint`, `leaves_mssql_installed`, `steps[]`.
- Only **two** endpoints take real input today: `failover` (`target`, `mode` query params) and
  `sync-rebuild` (`target`). Every other action endpoint is a bodyless trigger — its "request panel"
  legitimately has no fields, and the UI should say so plainly rather than implying hidden config.

---

## 2. Site Map / Routes

```
/                                    Landing — pick a database platform
/mssql                               MSSQL hub (4 phase cards)              [LIVE]
/mssql/build-ag                      Build AG                               [LIVE]
/mssql/rewind-teardown                Rewind / Teardown                     [LIVE]
/mssql/dr-failover                    DR Failover                          [LIVE]
/mssql/dr-sync                        DR Sync                              [LIVE]
/oracle                              Oracle hub                            [COMING SOON]
/oracle/build-ag                     Build AG (Data Guard)                 [COMING SOON]
/oracle/rewind-teardown               Rewind / Teardown                     [COMING SOON]
/oracle/dr-failover                   DR Failover (switchover/failover)    [COMING SOON]
/oracle/dr-sync                       DR Sync (reinstate standby)          [COMING SOON]
/mysql                               MySQL hub                             [COMING SOON]
/mysql/build-ag                      Build AG (replication)                [COMING SOON]
/mysql/rewind-teardown                Rewind / Teardown                     [COMING SOON]
/mysql/dr-failover                    DR Failover                          [COMING SOON]
/mysql/dr-sync                        DR Sync                              [COMING SOON]
```

- Route shape is identical for all three: `/:rdbms/:phase`, `rdbms ∈ {mssql, oracle, mysql}`,
  `phase ∈ {build-ag, rewind-teardown, dr-failover, dr-sync}` — one parameterized page template.
- Oracle/MySQL routes **render, they don't 404** — they show a `ComingSoonBanner` linking to the
  matching `docs/guides/*-design.md` doc instead of live action buttons.
- MSSQL calls `/api/v1/deploy/*` directly. Oracle/MySQL's proposed docs use a slightly different
  URL shape, so all endpoint paths live behind a **per-RDBMS API config object** (base URL, path
  per action) — wiring up a real backend later only touches that config, never the page components.

---

## 3. Landing Page — Wireframe

```
+--------------------------------------------------------------------------+
|  DevOps Console                                              [docs] [?]  |
+--------------------------------------------------------------------------+
|                                                                            |
|                Choose a database platform to manage                      |
|                                                                            |
|   +-------------------+   +-------------------+   +-------------------+  |
|   |  MSSQL            |   |  Oracle           |   |  MySQL            |  |
|   |  Always On AG     |   |  Data Guard       |   |  Replication      |  |
|   |                    |   |                    |   |                    |  |
|   |  [* ONLINE]        |   |  [COMING SOON]     |   |  [COMING SOON]     |  |
|   |  VM1 / VM2         |   |  VM3 / VM4        |   |  vm5 / vm6        |  |
|   |                    |   |                    |   |                    |  |
|   |  [   Open  >   ]   |   |  [  Preview  >  ]  |   |  [  Preview  >  ]  |  |
|   +-------------------+   +-------------------+   +-------------------+  |
|                                                                            |
+--------------------------------------------------------------------------+
```

`RdbmsCard`: green `[* ONLINE]` badge for MSSQL (backend live), zinc `[COMING SOON]` for Oracle and
MySQL (design docs only). All three cards are clickable — "Preview" leads into the same hub/phase
layout with disabled actions, so the full design can be reviewed before those backends exist.

---

## 4. RDBMS Hub Page — Wireframe (template, shown for `/mssql`)

```
+--------------------------------------------------------------------------+
|  <- Back          MSSQL — Always On Availability Group                   |
+--------------------------------------------------------------------------+
|  Hosts: VM1 [up]  VM2 [up]        Last action: full-ag  SUCCESS  09:14   |
+--------------------------------------------------------------------------+
|                                                                            |
|  +--------------+  +--------------+  +--------------+  +--------------+  |
|  | 1. Build AG  |  | 2. Rewind /  |  | 3. DR        |  | 4. DR Sync   |  |
|  |              |  |    Teardown  |  |    Failover  |  |              |  |
|  | install /    |  | teardown /   |  | failover     |  | sync-rebuild |  |
|  | build /      |  | rewind /     |  | (planned /   |  |              |  |
|  | restore /    |  | reset-       |  |  forced)     |  |              |  |
|  | backup /     |  | baseline     |  |              |  |              |  |
|  | alwayson     |  |              |  |              |  |              |  |
|  |              |  |              |  |              |  |              |  |
|  | [ Open >  ]  |  | [ Open >  ]  |  | [ Open >  ]  |  | [ Open >  ]  |  |
|  +--------------+  +--------------+  +--------------+  +--------------+  |
|                                                                            |
+--------------------------------------------------------------------------+
```

Below the phase cards, the hub also surfaces the two cross-cutting GET endpoints that don't belong
to one phase — `GET /hosts` and `GET /history` — as real widgets instead of hiding them behind a
generic "status" label:

```
+--------------------------------------------------------------------------+
|  Hosts                                    (HostsPanel · GET /hosts)      |
|  +----------------------+   +----------------------+                    |
|  | vm1          [up]    |   | vm2          [up]    |                    |
|  | host: devops_VM1     |   | host: devops_VM2     |                    |
|  | dns:  192.168.70.129 |   | dns:  192.168.70.130 |                    |
|  | ping: reachable      |   | ping: reachable      |                    |
|  +----------------------+   +----------------------+   [ Ping Hosts ]   |
+--------------------------------------------------------------------------+
|  Recent Executions                      (HistoryTable · GET /history)   |
|  task_id   operation      status     started        duration            |
|  8f2c1a…   alwayson       success    09:14:02        41.2s               |
|  c91e07…   failover-vm2   running    09:02:11        —                  |
+--------------------------------------------------------------------------+
```

Same layout reused for `/oracle` and `/mysql`, with their VM pairs in the host strip and each phase
card's action buttons disabled + a `ComingSoonBanner` at the top until real endpoints exist. Once
inside a phase page, `PhaseNavTabs` (sticky, under the header) lets the operator switch between the
4 phases without returning to the hub.

---

## 5. Phase Page Detail — the Action Card pattern

Every endpoint gets its own **Action Card**: a request panel (real fields, or an explicit
"no parameters" state) paired with a response panel showing the actual response envelope, not just
an endpoint label. One shared `TaskProgressPanel` + `LogTailPane` per page tracks whichever task is
currently active, since only one playbook run should be in flight at a time.

```
+--------------------------------------------------------------------------+
| MSSQL   [Build AG] [Rewind/Teardown] [DR Failover] [DR Sync]             |
+--------------------------------------------------------------------------+
| Form Always On AG                          [POST] /api/v1/deploy/alwayson|
| Configure Always-On AG across VM1 and VM2                                |
|----------------------------------------------------------------------- |
| REQUEST                                                                  |
|   No parameters required — triggers alwayson.yml against both replicas. |
|                                                          [ Run  ▷ ]      |
|----------------------------------------------------------------------- |
| RESPONSE                                                                 |
|   status      initiated              task_id     8f2c1a…                |
|   playbook    alwayson.yml           duration    ~30 min (estimated)    |
|   message     "Always On availability group deployment started"        |
|   operations  › Verify MSSQL connectivity                               |
|               › Create AG endpoints                                     |
|               › Create and join availability group                     |
|               › Verify AG health                                        |
|   ▸ View raw JSON                                                       |
+--------------------------------------------------------------------------+
```

This same card repeats for `Install`, `Build`, `Install Tools`, `Restore DB`, `Backup`,
`Run Full AG Sequence`, and `Check AG Status` — all bodyless, all sharing the response-envelope
shape above (only `message`, `playbook(s)`, and `operations`/`tags`/`notes` differ per action).

**Parameterized card** (the only two endpoints with real input — used on DR Failover / DR Sync):

```
+--------------------------------------------------------------------------+
| Fail Over                                 [POST] /api/v1/deploy/failover |
| Fail the AG over to target. planned = no data loss, forced = data-loss  |
| risk if source is unreachable.                                          |
|----------------------------------------------------------------------- |
| REQUEST                                                                  |
|   target  [ vm2 ▾ ]         mode  [ planned ▾ ]                         |
|                                                    [ Fail Over  ▷ ]      |
|----------------------------------------------------------------------- |
| RESPONSE                                                                 |
|   status   initiated     task_id  c91e07…    target  vm2   mode planned |
|   message  "Failover to vm2 (planned) started"                          |
|   ▸ View raw JSON                                                       |
+--------------------------------------------------------------------------+
```

**Interaction flow:** submitting a card's request (`Run` / `Fail Over` / `Sync / Rebuild`) fires the
real `POST`, the card's own response panel fills in from that call's JSON, and the returned
`task_id` is additionally handed to the page-level `TaskProgressPanel`, which polls
`GET /api/v1/deploy/status` every ~2s (falling back to matching the `task_id` in `GET /history`)
until `success`/`failed`, streaming lines into `LogTailPane`. While a task is `RUNNING`, every other
card's submit control on the page disables to prevent overlapping playbook runs.

### Other 3 phase pages

- **Rewind / Teardown** — `Teardown`, `Rewind`, `Reset Baseline` are bodyless Action Cards like
  `alwayson` above, but destructive, so their `Run` control routes through `ConfirmDangerDialog`
  first. `GET /rewind-plan` is **not** an Action Card (it triggers nothing) — it renders as a
  `RewindPlanAccordion`: three collapsible sections (Teardown / Rewind / Reset Baseline), each
  showing the real `steps[]` array from the response as a numbered list, fetched once on page load.
- **DR Failover** — the parameterized `failover` card above, plus a bodyless `Check AG Status` card
  (`POST /ag-status`) for pre-failover context.
- **DR Sync** — a parameterized `sync-rebuild` card (`target` only, no `mode`), plus the same
  `Check AG Status` card.

---

## 6. Reusable Component List

| Component | Purpose |
|---|---|
| `RdbmsCard` | Landing-page card linking to a hub; shows live/coming-soon badge + VM pair |
| `PhaseNavTabs` | Tab bar for switching between the 4 phases within an RDBMS hub |
| `HostStatusBar` | Compact strip on the hub header showing host up/down state + last action |
| `HostsPanel` | Hub widget rendering `GET /hosts` as host cards (name, host, dns, ping) + `Ping Hosts` |
| `HistoryTable` | Hub widget rendering `GET /history` as a sortable table of past task runs |
| `ActionCard` | The core unit: request panel + submit control + response panel for one endpoint |
| `RequestForm` | Renders an Action Card's input fields, or a `no-params` note when the endpoint takes none |
| `ResponseSummary` | Labeled key/value rendering of an Action Card's actual JSON response envelope |
| `JsonToggle` | Collapsible `<details>` under a `ResponseSummary` showing the raw JSON verbatim |
| `RewindPlanAccordion` | Renders `GET /rewind-plan`'s three static sections as expandable step lists |
| `TaskProgressPanel` | Page-level; polls status/history for the active `task_id`; progress bar + `StatusBadge` |
| `LogTailPane` | Auto-scrolling monospace log viewer sourced from `/history`, terminal-style chrome |
| `StatusBadge` | Colored pill: idle / running / success / failed / coming-soon |
| `ConfirmDangerDialog` | Typed-confirmation modal gating an Action Card's destructive submit |
| `TargetModeSelect` | Dropdown pair used inside the `failover`/`sync-rebuild` Action Cards' `RequestForm` |
| `ComingSoonBanner` | Placeholder banner on Oracle/MySQL pages, links to their design docs |

---

## 7. Dark-Mode Palette (Tailwind tokens)

| Role | Token | Hex |
|---|---|---|
| Page background | `slate-950` | `#020617` |
| Surface | `slate-900` | `#0f172a` |
| Card | `slate-800` | `#1e293b` |
| Border | `slate-700` | `#334155` |
| Muted text | `slate-400` | `#94a3b8` |
| Primary text | `slate-100` | `#f1f5f9` |
| Accent (buttons, links, active tab) | `sky-500` | `#0ea5e9` |
| Success | `emerald-500` | `#10b981` |
| Warning | `amber-500` | `#f59e0b` |
| Danger | `red-500` | `#ef4444` |
| Running / pending | `indigo-400` | `#818cf8` |

---

## 8. State / Data-Fetching Approach

Keep this minimal — no Redux/Zustand/React Query needed for v1:

- Each `ActionCard` owns its own local response state (the last JSON it received) plus its request
  field state (`target`/`mode` selects where applicable) — a card's response persists independently
  of other cards on the page.
- Page-level `useState` holds the single "active" `task_id` handed off to `TaskProgressPanel` /
  `LogTailPane` when a card is submitted (only one playbook run at a time).
- A single `usePolling(taskId, intervalMs)` hook wrapping `fetch` + timer, clearing itself on
  unmount or on reaching a terminal task status (`success`/`failed`).
- `HostsPanel` and `HistoryTable` use a plain `useEffect`-on-mount fetch (`GET /hosts`,
  `GET /history`) with a manual refresh button — no caching layer needed at this scale.
- The per-RDBMS API config object (base URL + endpoint paths) is the only "global" piece of state;
  plain React context or a simple module export is enough, no store library required.

---

## 9. Open Questions / Assumptions

- Should `reset-baseline`, `teardown`, and forced `failover` require **typed** confirmation (e.g.
  type "RESET") via `ConfirmDangerDialog`, or is a plain confirm sufficient?
- Confirmed direction here: Oracle/MySQL pages are **shown disabled** ("coming soon") rather than
  hidden, so the full design can be previewed end-to-end before those backends exist — flag if a
  fully-hidden approach is preferred instead.
- Is a single unified "Inventory / Hosts" page across all 3 RDBMS wanted, or is the per-hub
  `HostStatusBar` sufficient?
- Should the `LogTailPane` be paginated/filterable, or is "latest run's lines only" enough for v1?
- Any auth/RBAC requirement before exposing destructive actions on the network, given this console
  can trigger irreversible operations (`reset-baseline`, forced `failover`)?
- Today only `failover`/`sync-rebuild` accept input — every other action is fixed by `.env` config
  (SA password, version/edition, paths) per `app/config.py`. If the console should eventually let an
  operator override these per-request (e.g. picking `mssql_version`/`mssql_edition` from the UI),
  that requires adding real Pydantic request bodies to the backend first — out of scope for this
  design pass, but worth flagging since it would turn more Action Cards' `RequestForm`s into real
  forms instead of "no parameters" states.
