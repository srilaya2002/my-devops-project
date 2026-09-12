# React Frontend — Build From Scratch

A step-by-step implementation guide for the DevOps Console described in
`docs/design/frontend-ui-design.md` and prototyped in `docs/design/mockups/`. Follow this top to
bottom: install Node, scaffold the project, create the folders, then add each file in order. Every
code block names its exact path and explains why it lives there and how it connects to the rest.

**Scope of what you'll build:** the full site (landing + 3 RDBMS hubs + 4 phase pages each), wired
with real API calls against the **live** MSSQL backend (`python-fastapi-mssql`). Oracle and MySQL
render the same screens in a disabled "coming soon" state, exactly like the mockups — because
`config/rdbms.js` (the file that makes this possible) is written so flipping them on later is a data
change, not a rewrite.

---

## 1. Prerequisites — Node.js

Check what's already installed:

```bash
node -v
npm -v
```

You need Node **18 or newer**. If `node` isn't found, or the version is older, install it via
[nvm](https://github.com/nvm-sh/nvm) (works the same regardless of your exact Linux distro, so this
works whether you're doing this on your laptop or directly on `devops_VM1`):

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc          # or open a new shell
nvm install --lts
nvm use --lts
node -v                    # confirm v18+
npm -v
```

---

## 2. Create the project — directory structure

The frontend lives as a new top-level sibling to `python-fastapi-mssql/`, at the repo root. Scaffold
it with Vite's React template — this single command creates the base `frontend/` directory,
`package.json`, `index.html`, `vite.config.js`, and a starter `src/`:

```bash
cd /home/devops/devops/my-devops-project
npm create vite@latest frontend -- --template react
cd frontend
npm install
```

Vite's template doesn't know about our app's shape yet, so add the subfolders we're about to fill
in:

```bash
mkdir -p src/api src/hooks src/config src/components src/pages
```

**Why these folders, and not more:**

| Folder | Holds | Why separate |
|---|---|---|
| `src/api/` | The one function that talks to FastAPI | Every network call goes through one place — swap backends or add auth headers in one file |
| `src/hooks/` | `usePolling`, `useFetch` | Reusable data-fetching logic, independent of any one screen |
| `src/config/` | `rdbms.js` — the data model | The single source of truth for "what screens/actions exist"; pages read it, they don't hardcode it |
| `src/components/` | Presentational + interactive building blocks | Flat (no nested subfolders) — with ~18 small files this stays easy to scan; nesting would be premature |
| `src/pages/` | Route-level components | Only 3 files, because `PhasePage` is reused for all 4 phases × 3 RDBMS (see §14) |

By the end of this guide, the tree looks like this:

```
frontend/
├── .env.example
├── index.html
├── package.json
├── vite.config.js
└── src/
    ├── main.jsx
    ├── App.jsx
    ├── index.css
    ├── theme.css
    ├── api/
    │   └── client.js
    ├── hooks/
    │   ├── usePolling.js
    │   └── useFetch.js
    ├── config/
    │   └── rdbms.js
    ├── components/
    │   ├── TopBar.jsx
    │   ├── StatusBadge.jsx
    │   ├── MethodChip.jsx
    │   ├── ComingSoonBanner.jsx
    │   ├── ConfirmDangerDialog.jsx
    │   ├── ResponseSummary.jsx
    │   ├── ActionCard.jsx
    │   ├── TaskProgressPanel.jsx
    │   ├── LogTailPane.jsx
    │   ├── HostsPanel.jsx
    │   ├── HistoryTable.jsx
    │   ├── RewindPlanAccordion.jsx
    │   ├── RdbmsCard.jsx
    │   ├── PhaseCard.jsx
    │   └── PhaseNavTabs.jsx
    └── pages/
        ├── LandingPage.jsx
        ├── HubPage.jsx
        └── PhasePage.jsx
```

---

## 3. Install dependencies

```bash
cd frontend
npm install react-router-dom
npm install tailwindcss @tailwindcss/vite
```

- `react-router-dom` — client-side routing (`/`, `/:rdbms`, `/:rdbms/:phase`).
- `tailwindcss` + `@tailwindcss/vite` — Tailwind v4's Vite plugin. v4 needs no `tailwind.config.js`
  or PostCSS setup; it's one plugin line plus one `@import` in CSS (§7).

---

## 4. The dev-proxy decision (read this before writing any code)

The React dev server runs on `http://localhost:5173`; FastAPI runs on port `8000`. A browser `fetch`
from one origin to another is a **cross-origin request**, and `python-fastapi-mssql/app/main.py` has
no `CORSMiddleware` configured today — so a direct `fetch("http://<vm1>:8000/api/...")` from the
React app would be blocked by the browser.

Two ways to fix that: add CORS middleware to the live FastAPI service, or make Vite's dev server
**proxy** `/api/*` requests to FastAPI so the browser only ever talks to its own origin. This guide
uses the **proxy** — it needs zero changes to the FastAPI service that's already been verified
end-to-end against the real VMs (CLAUDE.md flags that path as live-tested; touching it isn't
necessary here). If you later deploy the built frontend somewhere that can't proxy (a static host
hitting FastAPI directly), you'd add `CORSMiddleware` to `main.py` at that point — not needed for
this guide.

### `frontend/vite.config.js`

Replace the generated file with:

```js
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || "http://localhost:8000";

  return {
    plugins: [react(), tailwindcss()],
    server: {
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
```

Every request the app makes to `/api/v1/deploy/...` is transparently forwarded server-side to
`proxyTarget` — the browser never sees FastAPI's origin, so there's no CORS problem at all.

### `frontend/.env.example`

```bash
# Where the Vite dev server proxies /api/* requests.
# - Frontend running on the same VM as FastAPI (e.g. on devops_VM1 itself): http://localhost:8000
# - Frontend running on a separate machine, FastAPI on devops_VM1:        http://192.168.70.129:8000
VITE_DEV_PROXY_TARGET=http://localhost:8000
```

Copy it: `cp .env.example .env`, then edit the value to match where you're running FastAPI from
(see CLAUDE.md's lab topology table — `devops_VM1` is `192.168.70.129`).

### `frontend/index.html`

Vite generates this; just change the `<title>`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>DevOps Console</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

---

## 5. Global styles — reuse the validated mockup CSS

The static HTML/CSS mockups already went through a full dark-mode design pass and were approved.
Don't redesign that here — port it as-is.

```bash
cp ../docs/design/mockups/styles.css frontend/src/theme.css
```

That file's per-database accent theming (`.theme-mssql`, `.theme-oracle`, `.theme-mysql`) was
written for the mockups' static HTML, where the class always landed on `<body>`. In the React app
we'll apply it to a wrapping `<div>` per page instead (§13–14), so open `src/theme.css` and drop the
`body` qualifier from those three rules:

```css
/* before: body.theme-mssql { ... }   after: */
.theme-mssql  { --accent: #3b82f6; --accent-2: #2563eb; --accent-glow: rgba(59,130,246,0.38); --brand-text: #93c5fd; }
.theme-oracle { --accent: #fb923c; --accent-2: #f97316; --accent-glow: rgba(251,146,60,0.35);  --brand-text: #fdba74; }
.theme-mysql  { --accent: #22d3ee; --accent-2: #06b6d4; --accent-glow: rgba(34,211,238,0.35);  --brand-text: #67e8f9; }
```

Then append two additions the mockups didn't need, because they were static screenshots:

```css
/* progress: indeterminate animation — the real API has no numeric progress,
   only queued/running/success/failed, so a fixed "62%" would be a lie. */
.progress-indeterminate {
  background: linear-gradient(90deg, transparent, var(--pending), var(--accent), transparent);
  background-size: 200% 100%;
  animation: indeterminate 1.4s ease-in-out infinite;
}
@keyframes indeterminate {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* confirm-danger dialog — the mockups only had static text describing this;
   the real app needs an actual modal. */
.dialog-backdrop {
  position: fixed; inset: 0;
  background: rgba(2,4,9,0.72);
  display: flex; align-items: center; justify-content: center;
  z-index: 50;
}
.dialog {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 22px;
  width: 320px;
  box-shadow: var(--shadow-lg);
}
.dialog-title { margin: 0 0 8px; font-size: 15px; font-weight: 700; }
.dialog-body { margin: 0 0 12px; font-size: 12.5px; color: var(--muted); }
.dialog-input {
  width: 100%;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 14px;
}
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
```

### `frontend/src/index.css`

Replace the generated file with:

```css
@import "tailwindcss";
@import "./theme.css";

:root {
  color-scheme: dark;
}

html, body, #root {
  height: 100%;
}
```

Tailwind is imported for utility classes you might reach for later (spacing tweaks, one-off
layout); `theme.css` supplies the actual design system — the two coexist without conflict since
`theme.css` only defines named classes (`.card`, `.action-card`, `.btn`, …), never bare utilities.

---

## 6. The data model — the heart of the app

Before any component, build the file every page reads from. This is what makes 3 databases × 4
phases × up to 8 actions each (36 screens' worth of content) come from **one** `PhasePage` component
instead of 12 near-duplicate files: every screen is a lookup into this object, not a page of its own.

### `frontend/src/config/rdbms.js`

```js
export const RDBMS = [
  {
    id: "mssql",
    label: "MSSQL",
    subtitle: "Always On Availability Group",
    live: true,
    apiBase: "/api/v1/deploy",
    docHref: null,
    hosts: [
      { key: "vm1", label: "VM1", host: "devops_VM1" },
      { key: "vm2", label: "VM2", host: "devops_VM2" },
    ],
    phases: {
      "build-ag": {
        title: "Build AG",
        actions: [
          { key: "install", label: "Install", method: "POST", path: "/install",
            description: "Deploy and install MSSQL Server on both replicas (30–60 min)." },
          { key: "build", label: "Build", method: "POST", path: "/build",
            description: "Idempotent prepare + install via the mssql_build role." },
          { key: "install-tools", label: "Install Tools", method: "POST", path: "/install-tools",
            description: "Install sqlcmd / mssql-tools only." },
          { key: "restore-db", label: "Restore DB", method: "POST", path: "/restore-db",
            description: "Restore AdventureWorks to VM1 only." },
          { key: "backup", label: "Backup", method: "POST", path: "/backup",
            description: "10-stripe backup on VM1, transferred and restored on VM2." },
          { key: "alwayson", label: "Form Always On AG", method: "POST", path: "/alwayson",
            description: "Configure the Always On Availability Group across VM1 and VM2." },
          { key: "full-ag", label: "Run Full AG Sequence", method: "POST", path: "/full-ag",
            description: "Chains restore → striped backup/restore → Always On." },
          { key: "ag-status", label: "Check AG Status", method: "POST", path: "/ag-status", primary: false,
            description: "Read-only snapshot of replica roles and sync state." },
        ],
      },
      "rewind-teardown": {
        title: "Rewind / Teardown",
        showPlan: true,
        actions: [
          { key: "teardown", label: "Teardown", method: "POST", path: "/teardown", danger: true,
            confirmText: "TEARDOWN",
            description: "Drop AG, AdventureWorks, and backup artifacts. Leaves MSSQL installed." },
          { key: "rewind", label: "Rewind", method: "POST", path: "/rewind", danger: true,
            confirmText: "REWIND",
            description: "Teardown, then reinstall a clean AdventureWorks on VM1 only." },
          { key: "reset-baseline", label: "Reset Baseline", method: "POST", path: "/reset-baseline", danger: true,
            confirmText: "RESET",
            description: "Full uninstall — returns both hosts to a bare VM." },
        ],
      },
      "dr-failover": {
        title: "DR Failover",
        actions: [
          { key: "ag-status", label: "Check AG Status", method: "POST", path: "/ag-status", primary: false,
            description: "Confirm current primary/secondary and sync state before failing over." },
          { key: "failover", label: "Fail Over", method: "POST", path: "/failover", danger: true,
            confirmText: "FORCE", confirmWhen: (values) => values.mode === "forced",
            description: "planned requires target already SYNCHRONIZED. forced risks data loss.",
            params: [
              { name: "target", label: "target", options: ["vm1", "vm2"], default: "vm2" },
              { name: "mode", label: "mode", options: ["planned", "forced"], default: "planned" },
            ] },
        ],
      },
      "dr-sync": {
        title: "DR Sync",
        actions: [
          { key: "ag-status", label: "Check AG Status", method: "POST", path: "/ag-status", primary: false,
            description: "Confirm which replica is lagging or suspended before resyncing." },
          { key: "sync-rebuild", label: "Sync / Rebuild", method: "POST", path: "/sync-rebuild",
            description: "Resumes suspended data movement, or rejoins and reseeds the target.",
            params: [
              { name: "target", label: "target", options: ["vm1", "vm2"], default: "vm2" },
            ] },
        ],
      },
    },
  },
  {
    id: "oracle",
    label: "Oracle",
    subtitle: "19c Data Guard",
    live: false,
    apiBase: null,
    docHref: "docs/guides/oracle-19c-dataguard-design.md",
    hosts: [
      { key: "vm3", label: "VM3", host: "devops_VM3" },
      { key: "vm4", label: "VM4", host: "devops_VM4" },
    ],
    phases: {
      "build-ag": { title: "Build AG", actions: [
        { key: "install", label: "Install", method: "POST", path: "/install", description: "Install Oracle 19c software on VM3 + VM4." },
        { key: "listener", label: "Configure Listener", method: "POST", path: "/listener", description: "Configure the TNS listener on both hosts." },
        { key: "primary-db", label: "Primary DB (vm3)", method: "POST", path: "/primary-db", description: "Create the primary database on VM3." },
        { key: "standby-prep", label: "Standby Prep (vm4)", method: "POST", path: "/standby-prep", description: "Prepare the standby instance on VM4." },
        { key: "duplicate-standby", label: "Duplicate Standby", method: "POST", path: "/duplicate-standby", description: "RMAN duplicate from primary to build the standby database." },
        { key: "dataguard-broker", label: "Form Data Guard Broker", method: "POST", path: "/dataguard-broker", description: "Enable the broker configuration and register both instances." },
      ]},
      "rewind-teardown": { title: "Rewind / Teardown", showPlan: true, actions: [
        { key: "teardown", label: "Teardown", method: "POST", path: "/teardown", danger: true, description: "Remove Data Guard config and standby, keep Oracle installed." },
        { key: "rewind", label: "Rewind", method: "POST", path: "/rewind", danger: true, description: "Teardown, then rebuild a fresh primary on VM3." },
        { key: "reset-baseline", label: "Reset Baseline", method: "POST", path: "/reset-baseline", danger: true, description: "Deinstall Oracle entirely on both hosts." },
      ]},
      "dr-failover": { title: "DR Failover", actions: [
        { key: "failover", label: "Switchover / Failover", method: "POST", path: "/failover", danger: true,
          description: "switchover = planned, no data loss. failover = forced.",
          params: [
            { name: "target", label: "target", options: ["vm3", "vm4"], default: "vm4" },
            { name: "mode", label: "mode", options: ["switchover", "forced failover"], default: "switchover" },
          ] },
      ]},
      "dr-sync": { title: "DR Sync", actions: [
        { key: "sync-rebuild", label: "Reinstate Standby", method: "POST", path: "/sync-rebuild",
          description: "Resync or reinstate a lagging/dropped-out standby.",
          params: [ { name: "target", label: "target", options: ["vm3", "vm4"], default: "vm4" } ] },
      ]},
    },
  },
  {
    id: "mysql",
    label: "MySQL",
    subtitle: "8.4 XtraBackup Replication",
    live: false,
    apiBase: null,
    docHref: "docs/guides/mysql-8-4-10-xtrabackup-replication-design.md",
    hosts: [
      { key: "vm5", label: "VM5", host: "devops_VM5" },
      { key: "vm6", label: "VM6", host: "devops_VM6" },
    ],
    phases: {
      "build-ag": { title: "Build AG", actions: [
        { key: "install", label: "Install", method: "POST", path: "/install", description: "Install MySQL 8.4 server on vm5 + vm6." },
        { key: "configure", label: "Configure", method: "POST", path: "/configure", description: "Apply GTID replication + server-id configuration." },
        { key: "replication-user", label: "Create Replication User (vm5)", method: "POST", path: "/replication-user", description: "Create the GTID replication account on the primary." },
        { key: "backup", label: "Backup (vm5)", method: "POST", path: "/backup", description: "XtraBackup full backup on the primary." },
        { key: "restore", label: "Restore (vm6)", method: "POST", path: "/restore", description: "Transfer and prepare the backup on the replica." },
        { key: "replication", label: "Start Replication (vm6)", method: "POST", path: "/replication", description: "Point the replica at the primary and start GTID replication." },
      ]},
      "rewind-teardown": { title: "Rewind / Teardown", showPlan: true, actions: [
        { key: "teardown", label: "Teardown", method: "POST", path: "/teardown", danger: true, description: "Stop replication and reset GTID state." },
        { key: "rewind", label: "Rewind", method: "POST", path: "/rewind", danger: true, description: "Teardown, then rebuild replication from a fresh backup." },
        { key: "reset-baseline", label: "Reset Baseline", method: "POST", path: "/reset-baseline", danger: true, description: "Uninstall MySQL entirely on both hosts." },
      ]},
      "dr-failover": { title: "DR Failover", actions: [
        { key: "failover", label: "Switchover / Failover", method: "POST", path: "/failover", danger: true,
          description: "planned = graceful promotion. forced = promote immediately.",
          params: [
            { name: "target", label: "target", options: ["vm5", "vm6"], default: "vm6" },
            { name: "mode", label: "mode", options: ["planned", "forced"], default: "planned" },
          ] },
      ]},
      "dr-sync": { title: "DR Sync", actions: [
        { key: "sync-rebuild", label: "Reconnect Replica", method: "POST", path: "/sync-rebuild",
          description: "Resync or reconnect a broken/lagging replica via a fresh XtraBackup cycle.",
          params: [ { name: "target", label: "target", options: ["vm5", "vm6"], default: "vm6" } ] },
      ]},
    },
  },
];

export const PHASE_ORDER = ["build-ag", "rewind-teardown", "dr-failover", "dr-sync"];

export function getRdbms(id) {
  return RDBMS.find((r) => r.id === id);
}
```

**Field meanings, since every component downstream trusts this shape:**

- `live` — gates everything: whether `ActionCard` submits become real fetches, whether the hub's
  `HostsPanel`/`HistoryTable` fetch anything, whether `TaskProgressPanel`/`LogTailPane` render at all.
- `apiBase` — prefixed onto every action's `path` before it hits `src/api/client.js`. `null` for
  Oracle/MySQL because there's nothing to call yet.
- `params` — presence/absence is what tells `ActionCard` whether to render `<select>` fields or a
  plain "no parameters required" line. Only `failover` and `sync-rebuild` have it, because those are
  the only two real endpoints (`python-fastapi-mssql/app/routes/deploy.py`) that take input.
- `danger` / `confirmText` / `confirmWhen` — gates the typed-confirmation dialog. `confirmWhen` lets
  `failover` be dangerous only when `mode=forced` is selected, not for `planned`.
- `showPlan` — the one per-phase flag `PhasePage` checks to decide whether to render
  `RewindPlanAccordion` above the action cards.
- `primary: false` — the only styling hint the config carries; `ag-status` actions render as a plain
  button instead of the blue "primary" one, since they're read-only checks, not the main action of
  the page.

---

## 7. The API layer — one function, one place

Every network call in the app funnels through this. Nothing else in `src/` calls `fetch` directly.

### `frontend/src/api/client.js`

```js
const DEFAULT_TIMEOUT_MS = 15000;

export async function apiRequest(base, path, { method = "GET", params, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const url = new URL(base + path, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null) url.searchParams.set(key, value);
    });
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url.pathname + url.search, {
      method,
      signal: controller.signal,
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const message = body?.detail || `${method} ${path} failed (${res.status})`;
      throw new Error(message);
    }
    return body;
  } finally {
    clearTimeout(timeout);
  }
}
```

Two details worth understanding:

- We build a `URL` object just to get clean query-string handling (`searchParams.set`), then fetch
  using only `url.pathname + url.search` — a **relative** path like `/api/v1/deploy/failover?target=vm2`.
  That's what lets Vite's proxy (§4) intercept it; an absolute `http://...` URL would bypass the
  proxy and hit the CORS wall directly.
- `params` becomes **query string** parameters, not a JSON body — because that's literally what
  `failover`/`sync-rebuild` expect (`app/routes/deploy.py`: `async def deploy_failover(..., target: str, mode: str = "planned")`
  are plain function parameters, which FastAPI treats as query params when there's no request-body
  model). This client mirrors the real backend's contract instead of guessing at one.

---

## 8. Hooks — polling and one-shot fetching

Two small hooks cover every data need in the app: repeatedly polling something until it reaches a
terminal state (`usePolling`), and fetching something once on mount with a manual refresh
(`useFetch`).

### `frontend/src/hooks/usePolling.js`

```js
import { useEffect, useRef, useState } from "react";

export function usePolling(fetcher, { intervalMs = 2000, enabled = true, stopWhen } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const fetcherRef = useRef(fetcher);
  const stopWhenRef = useRef(stopWhen);
  fetcherRef.current = fetcher;
  stopWhenRef.current = stopWhen;

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    let timer;

    async function tick() {
      try {
        const result = await fetcherRef.current();
        if (cancelled) return;
        setData(result);
        setError(null);
        if (stopWhenRef.current && stopWhenRef.current(result)) return;
      } catch (err) {
        if (!cancelled) setError(err);
      }
      if (!cancelled) timer = setTimeout(tick, intervalMs);
    }

    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [enabled, intervalMs]);

  return { data, error };
}
```

**Why `fetcherRef`/`stopWhenRef` instead of just using the arguments directly:** callers pass inline
arrow functions (`() => apiRequest(...)`), which are a *new* function on every render. If those were
listed in the `useEffect` dependency array, the polling loop would tear down and restart on every
single render — not broken, but wasteful and jittery. Storing them in a `ref` and updating the ref
every render means the effect only re-runs when `enabled` or `intervalMs` actually change, while
`tick()` always calls the *latest* version via `fetcherRef.current()`. This ref-for-latest-closure
pattern is worth recognizing — it comes up constantly once you're polling or debouncing in React.

### `frontend/src/hooks/useFetch.js`

```js
import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../api/client";

export function useFetch(base, path, { enabled = true } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    try {
      const result = await apiRequest(base, path);
      setData(result);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [base, path, enabled]);

  useEffect(() => { refresh(); }, [refresh]);

  return { data, error, loading, refresh };
}
```

Used for things that don't need live polling: `GET /hosts`, `GET /history` (on the hub page), and
`GET /rewind-plan`. Each also exposes `refresh` for a manual "Refresh" button.

---

## 9. Small presentational components

These have no state and no network calls — pure rendering, reused everywhere.

### `frontend/src/components/StatusBadge.jsx`

```jsx
const VARIANTS = {
  live: "badge-live",
  running: "badge-run",
  success: "badge-live",
  failed: "badge-fail",
  idle: "badge-idle",
  soon: "badge-soon",
};

export function StatusBadge({ status, children }) {
  const variant = VARIANTS[status] || "badge-idle";
  return <span className={`badge ${variant}`}>{children ?? status}</span>;
}
```

### `frontend/src/components/MethodChip.jsx`

```jsx
export function MethodChip({ method, path }) {
  const cls = method === "GET" ? "method-get" : "method-post";
  return <span className={`method-chip ${cls}`}>{method} {path}</span>;
}
```

### `frontend/src/components/ComingSoonBanner.jsx`

```jsx
export function ComingSoonBanner({ docPath }) {
  return (
    <div className="banner-soon">
      Backend not yet implemented
      {docPath && <> — see <code>{docPath}</code> in the repo.</>}
    </div>
  );
}
```

`docPath` here is plain text (`<code>`), not a link — Vite's dev server doesn't serve arbitrary
repo markdown files, so a real `<a href>` to `docs/guides/...` would 404. If you later add a docs
viewer route, this is the one place to change.

### `frontend/src/components/ConfirmDangerDialog.jsx`

```jsx
import { useState } from "react";

export function ConfirmDangerDialog({ open, confirmText, actionLabel, onConfirm, onCancel }) {
  const [value, setValue] = useState("");
  if (!open) return null;
  const matches = value.trim() === confirmText;

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog">
        <p className="dialog-title">Confirm {actionLabel}</p>
        <p className="dialog-body">
          Type <strong>{confirmText}</strong> to confirm. This action cannot be undone.
        </p>
        <input className="dialog-input" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        <div className="dialog-actions">
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button className="btn btn-danger" disabled={!matches} onClick={onConfirm}>
            {actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
```

Note there's no separate `JsonToggle` or `TargetModeSelect` component file — both got folded into
`ActionCard`/`ResponseSummary` (§11) because each is only ever used in exactly one place. Splitting
them out would be an abstraction with no second caller, which is exactly the kind of premature split
worth avoiding.

### `frontend/src/components/ResponseSummary.jsx`

Renders **any** POST response envelope generically — it doesn't know about `alwayson` vs `failover`
vs `backup`; it just reads whatever keys came back. That's possible because every real response
shares the same envelope shape (`status`, `task_id`, `message`, `playbook`, arrays like
`operations`, …) — see `python-fastapi-mssql/app/routes/deploy.py`.

```jsx
function isPrimitiveEntry([, value]) {
  return typeof value !== "object" || value === null;
}

export function ResponseSummary({ response }) {
  if (!response) {
    return <p className="page-sub" style={{ margin: 0 }}>Not run yet in this session.</p>;
  }

  const entries = Object.entries(response);
  const primitive = entries.filter(isPrimitiveEntry);
  const lists = entries.filter(([, value]) => Array.isArray(value));

  return (
    <>
      <dl className="response-grid">
        {primitive.map(([key, value]) => (
          <Row key={key} label={key} value={String(value)} />
        ))}
      </dl>
      {lists.map(([key, value]) => (
        <div key={key}>
          <p className="block-label" style={{ marginTop: 10 }}>{key}</p>
          <ul className="checklist">
            {value.map((item, i) => <li key={i}>{typeof item === "string" ? item : JSON.stringify(item)}</li>)}
          </ul>
        </div>
      ))}
      <details className="json-toggle">
        <summary>View raw JSON</summary>
        <pre>{JSON.stringify(response, null, 2)}</pre>
      </details>
    </>
  );
}

function Row({ label, value }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
```

The `<details>`/`<summary>` "View raw JSON" toggle needs zero JavaScript state — that's the browser's
native disclosure widget, styled by `theme.css`'s `.json-toggle` rules.

---

## 10. `ActionCard` — the core component

This is the piece that turns each entry in `config/rdbms.js` into a real, working request/response
card. Everything else in the app exists to feed data into or read state out of this component.

### `frontend/src/components/ActionCard.jsx`

```jsx
import { useState } from "react";
import { apiRequest } from "../api/client";
import { MethodChip } from "./MethodChip";
import { ResponseSummary } from "./ResponseSummary";
import { ConfirmDangerDialog } from "./ConfirmDangerDialog";

export function ActionCard({ action, apiBase, live, disabled, onStarted }) {
  const [values, setValues] = useState(() =>
    Object.fromEntries((action.params ?? []).map((p) => [p.name, p.default]))
  );
  const [response, setResponse] = useState(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const [confirming, setConfirming] = useState(false);

  const isDanger = action.danger && (!action.confirmWhen || action.confirmWhen(values));

  async function run() {
    setPending(true);
    setError(null);
    try {
      const result = await apiRequest(apiBase, action.path, {
        method: action.method,
        params: action.params ? values : undefined,
      });
      setResponse(result);
      if (result?.task_id) onStarted?.(result.task_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setPending(false);
    }
  }

  function handleSubmit() {
    if (isDanger) setConfirming(true);
    else run();
  }

  return (
    <div className={`action-card${disabled ? " disabled" : ""}`}>
      <div className="action-card-head">
        <div>
          <h4>{action.label}</h4>
          <p className="action-card-desc">{action.description}</p>
        </div>
        <MethodChip method={action.method} path={apiBase ? `${apiBase}${action.path}` : action.path} />
      </div>
      <div className="action-card-body">
        <div className="request-block">
          <p className="block-label">Request</p>
          <div className="request-form-row">
            {action.params ? (
              <div className="field-group">
                {action.params.map((p) => (
                  <div className="field" key={p.name}>
                    <label>{p.label}</label>
                    <select
                      disabled={disabled}
                      value={values[p.name]}
                      onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                    >
                      {p.options.map((opt) => <option key={opt}>{opt}</option>)}
                    </select>
                  </div>
                ))}
              </div>
            ) : (
              <p className="no-params">No parameters required.</p>
            )}
            <button
              className={`btn ${isDanger ? "btn-danger" : action.primary === false ? "" : "btn-primary"}`}
              disabled={disabled || pending}
              onClick={handleSubmit}
            >
              {pending ? "Running…" : `${action.label} ▷`}
            </button>
          </div>
          {error && <p className="page-sub" style={{ color: "var(--danger)" }}>{error}</p>}
        </div>
        <div className="response-block">
          <p className="block-label">Response</p>
          {live ? <ResponseSummary response={response} /> : (
            <p className="page-sub" style={{ margin: 0 }}>Not available — backend not implemented.</p>
          )}
        </div>
      </div>
      {action.danger && (
        <ConfirmDangerDialog
          open={confirming}
          confirmText={action.confirmText ?? action.label.toUpperCase()}
          actionLabel={action.label}
          onCancel={() => setConfirming(false)}
          onConfirm={() => { setConfirming(false); run(); }}
        />
      )}
    </div>
  );
}
```

**How to read this component:**

- `values` state only exists for actions with `params` — most actions have none, so `values` is
  `{}` and the request panel renders the "No parameters required" line instead of any fields.
- `isDanger` is computed fresh every render from `action.danger` and the optional `confirmWhen(values)`.
  This is why `failover`'s button only turns red, and only demands typed confirmation, once you've
  actually selected `mode = forced` — for `planned` it submits immediately.
- On a successful call, two things happen with the one response: `setResponse(result)` feeds this
  card's own `ResponseSummary`, and `onStarted?.(result.task_id)` bubbles the `task_id` up to the
  page (§14), which is how the page-level progress panel and log pane learn what to track. A single
  fetch, two consumers — the card doesn't know or care what the page does with the `task_id`.
- `live` (not `disabled`) controls whether `ResponseSummary` renders at all — Oracle/MySQL cards are
  interactive-looking but permanently `disabled` *and* `!live`, so clicking does nothing and the
  response area always reads "backend not implemented."
- `disabled` is passed in from the page, not computed here — see §14 for why (one task running at a
  time, page-wide).

---

## 11. `TaskProgressPanel` and `LogTailPane`

These are page-level, not per-card — only one playbook run should be tracked at a time, so they sit
below the list of `ActionCard`s and follow whichever `task_id` the page currently has active.

### `frontend/src/components/TaskProgressPanel.jsx`

```jsx
import { useEffect } from "react";
import { usePolling } from "../hooks/usePolling";
import { apiRequest } from "../api/client";
import { StatusBadge } from "./StatusBadge";

const TERMINAL = ["success", "failed"];

export function TaskProgressPanel({ apiBase, taskId, onStatusChange }) {
  const { data } = usePolling(
    () => apiRequest(apiBase, "/status"),
    {
      intervalMs: 2000,
      enabled: Boolean(taskId),
      stopWhen: (result) => TERMINAL.includes(result?.latest_task?.status),
    }
  );

  const task = data?.latest_task;
  const status = task?.task_id === taskId ? task.status : "idle";
  const running = status === "running" || status === "queued";

  useEffect(() => {
    onStatusChange?.(status);
  }, [status, onStatusChange]);

  return (
    <div className="panel">
      <p className="panel-title">Task Progress</p>
      <div className="progress-meta">
        <span>
          task_id: <span className="mono">{taskId ? shorten(taskId) : "—"}</span>
          {task?.operation && <> · operation: <span className="mono">{task.operation}</span></>}
        </span>
        <StatusBadge status={running ? "running" : status}>{status.toUpperCase()}</StatusBadge>
      </div>
      <div className="progress-track">
        <div
          className={`progress-fill${running ? " progress-indeterminate" : ""}`}
          style={{ width: running ? "40%" : status === "success" || status === "failed" ? "100%" : "0%" }}
        />
      </div>
    </div>
  );
}

function shorten(id) { return id.length > 12 ? `${id.slice(0, 8)}…` : id; }
```

`GET /api/v1/deploy/status` only ever reports the **latest** task, so `status === task?.task_id === taskId ? task.status : "idle"`
guards against showing stale progress for a task that isn't the one this page just started (e.g. if
someone else — or another tab — kicked off a different task in between). This component deliberately
does **not** show a fabricated percentage: the real API only exposes `queued` / `running` / `success`
/ `failed`, so `running` gets the `.progress-indeterminate` striped animation (added to `theme.css`
in §5) instead of a made-up number — the static mockups used fixed percentages like `62%` purely for
illustration, and that's not something the real component should carry over.

`onStatusChange` is how the *page* (§14) learns the task is running, so it can disable every other
`ActionCard` on the page — see the next section for why this is a callback rather than the page
polling `/status` a second time.

### `frontend/src/components/LogTailPane.jsx`

```jsx
import { usePolling } from "../hooks/usePolling";
import { apiRequest } from "../api/client";

const TERMINAL = ["success", "failed"];

export function LogTailPane({ apiBase, taskId }) {
  const { data } = usePolling(
    () => apiRequest(apiBase, "/history"),
    {
      intervalMs: 2000,
      enabled: Boolean(taskId),
      stopWhen: (result) => {
        const entry = result?.executions?.find((e) => e.task_id === taskId);
        return TERMINAL.includes(entry?.status);
      },
    }
  );

  const entry = data?.executions?.find((e) => e.task_id === taskId);
  const stdout = entry?.results?.stdout;

  return (
    <div className="panel">
      <p className="panel-title">Log Tail</p>
      <div className="log-pane">
        <div className="log-chrome"><span /><span /><span /></div>
        <div className="log-body">
          {entry?.error && <div style={{ color: "var(--danger)" }}>{entry.error}</div>}
          {stdout
            ? stdout.trim().split("\n").slice(-40).map((line, i) => <div key={i}>{line}</div>)
            : <span className="dim">{taskId ? "waiting for playbook output…" : "no task run yet — GET /api/v1/deploy/history"}</span>}
        </div>
      </div>
    </div>
  );
}
```

This polls `/history` (not `/status`) because `stdout` only lives on a history entry's `results`
field, populated once `AnsibleMssqlDeployer._run_task` finishes and calls `_update(..., results=result)`
(`app/deployer.py`) — while a task is running, `results` is still `None`, hence the "waiting for
playbook output…" fallback. `.slice(-40)` keeps only the last 40 lines so a long Ansible run doesn't
turn the pane into an unbounded, unreadable wall of text.

Yes, `TaskProgressPanel` and `LogTailPane` each run their own independent 2-second poll, against two
different endpoints (`/status` vs `/history`). At this app's scale that's a fine trade-off — two
small polling loops are simpler to read than a shared task-store abstraction, and the design doc's
own guidance (§8, "keep state minimal") explicitly favors this. If the app grows more screens that
need the same task data, that's the point where it'd be worth lifting both into one shared poll.

---

## 12. Hub-page widgets

### `frontend/src/components/HostsPanel.jsx`

```jsx
import { useState } from "react";
import { useFetch } from "../hooks/useFetch";
import { apiRequest } from "../api/client";
import { StatusBadge } from "./StatusBadge";

export function HostsPanel({ apiBase, live }) {
  const { data } = useFetch(apiBase, "/hosts", { enabled: live });
  const [pingResults, setPingResults] = useState(null);
  const [pinging, setPinging] = useState(false);

  async function ping() {
    setPinging(true);
    try {
      const result = await apiRequest(apiBase, "/ping", { method: "POST" });
      setPingResults(result.results);
    } finally {
      setPinging(false);
    }
  }

  const hosts = data?.inventory?.hosts ?? [];
  const dns = data?.dns ?? {};

  return (
    <div className="panel">
      <p className="panel-title">Hosts</p>
      <div className="host-grid">
        {hosts.length === 0 && (
          <p className="page-sub" style={{ margin: 0 }}>
            {live ? "Loading…" : "No hosts — backend not implemented."}
          </p>
        )}
        {hosts.map((h) => {
          const dnsInfo = dns[h.host];
          const pingInfo = pingResults?.find((r) => r.host === h.host);
          return (
            <div className="host-card" key={h.name}>
              <div className="host-name">
                {h.name}
                <StatusBadge status={pingInfo ? (pingInfo.status === "reachable" ? "live" : "failed") : "idle"}>
                  {pingInfo?.status ?? "unknown"}
                </StatusBadge>
              </div>
              <dl>
                <dt>host</dt><dd>{h.host}</dd>
                <dt>user</dt><dd>{h.user}</dd>
                <dt>port</dt><dd>{h.port}</dd>
                <dt>dns</dt><dd>{dnsInfo?.resolved ? dnsInfo.address : "unresolved"}</dd>
              </dl>
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 14, display: "flex", justifyContent: "flex-end" }}>
        <button className="btn" disabled={!live || pinging} onClick={ping}>
          {pinging ? "Pinging…" : "Ping Hosts ▷"}
        </button>
      </div>
    </div>
  );
}
```

`hosts` comes from `GET /hosts`'s `inventory.hosts` array, keyed by `name` (`vm1`/`vm2`); `dns` is a
separate map keyed by **hostname** (`devops_VM1`), not by `name` — that mismatch is exactly how
`app/deployer.py`'s `get_hosts()`/`resolve_hosts()` shape their responses, so `dns[h.host]` (not
`dns[h.name]`) is required to line them up correctly.

### `frontend/src/components/HistoryTable.jsx`

```jsx
import { useFetch } from "../hooks/useFetch";
import { StatusBadge } from "./StatusBadge";

export function HistoryTable({ apiBase, live }) {
  const { data, refresh, loading } = useFetch(apiBase, "/history", { enabled: live });
  const executions = data?.executions ?? [];

  return (
    <div className="panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <p className="panel-title" style={{ marginBottom: 14 }}>Recent Executions</p>
        {live && <button className="btn" onClick={refresh}>{loading ? "Refreshing…" : "Refresh"}</button>}
      </div>
      {executions.length === 0 ? (
        <p className="page-sub" style={{ margin: 0 }}>
          {live ? "No executions yet." : "No executions — backend not implemented."}
        </p>
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>task_id</th><th>operation</th><th>status</th><th>started</th><th>duration</th></tr>
          </thead>
          <tbody>
            {executions.slice(0, 10).map((e) => (
              <tr key={e.task_id}>
                <td className="mono">{e.task_id.slice(0, 8)}…</td>
                <td>{e.operation}</td>
                <td><StatusBadge status={e.status === "success" ? "live" : e.status}>{e.status}</StatusBadge></td>
                <td className="mono">{new Date(e.started_at).toLocaleTimeString()}</td>
                <td>{e.results?.duration_seconds ? `${e.results.duration_seconds.toFixed(1)}s` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

`executions` is already newest-first — `AnsibleMssqlDeployer._record()` does `self._history.insert(0, record)`
— so no client-side sorting is needed; `.slice(0, 10)` just caps how many rows render.

### `frontend/src/components/RewindPlanAccordion.jsx`

```jsx
import { useFetch } from "../hooks/useFetch";

export function RewindPlanAccordion({ apiBase, live }) {
  const { data } = useFetch(apiBase, "/rewind-plan", { enabled: live });

  if (!live) {
    return (
      <div className="panel">
        <p className="panel-title">Rewind Plan</p>
        <p className="page-sub" style={{ margin: 0 }}>Not available — backend not implemented.</p>
      </div>
    );
  }

  const sections = data ? Object.entries(data).filter(([key]) => key !== "note") : [];

  return (
    <div className="panel">
      <p className="panel-title">Rewind Plan</p>
      {sections.map(([key, section]) => (
        <details className="accordion-item" key={key}>
          <summary>
            {titleCase(key)}
            <span className={`badge ${section.leaves_mssql_installed === false ? "badge-fail" : "badge-idle"}`}>
              {section.leaves_mssql_installed === false ? "removes install" : "leaves install"}
            </span>
          </summary>
          <div className="accordion-body">
            <ol>{section.steps.map((step, i) => <li key={i}>{step}</li>)}</ol>
          </div>
        </details>
      ))}
    </div>
  );
}

function titleCase(key) {
  return key.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
```

Unlike the static mockup — which had to hand-type each step as literal HTML — this renders directly
from whatever `GET /api/v1/deploy/rewind-plan` returns (`app/deployer.py`'s `get_rewind_plan()`). If
that method's step text ever changes, this component needs no edit at all.

---

## 13. Navigation components

### `frontend/src/components/TopBar.jsx`

```jsx
import { Link } from "react-router-dom";

export function TopBar({ docHref = "docs/design/frontend-ui-design.md" }) {
  return (
    <div className="topbar">
      <div className="brand"><Link to="/">DevOps Console</Link></div>
      <div className="doc-ref">Design spec: <code>{docHref}</code></div>
    </div>
  );
}
```

### `frontend/src/components/RdbmsCard.jsx`

```jsx
import { Link } from "react-router-dom";

export function RdbmsCard({ rdbms }) {
  return (
    <div className={`card theme-${rdbms.id}${rdbms.live ? "" : " disabled"}`}>
      <span className={`badge ${rdbms.live ? "badge-live" : "badge-soon"}`}>
        {rdbms.live ? "ONLINE" : "COMING SOON"}
      </span>
      <h3>{rdbms.label}</h3>
      <p className="meta">{rdbms.subtitle}</p>
      <p className="meta">{rdbms.hosts.map((h) => h.label).join(" / ")}</p>
      <div className="spacer" />
      <Link className="btn btn-primary" to={`/${rdbms.id}`}>
        {rdbms.live ? "Open >" : "Preview >"}
      </Link>
    </div>
  );
}
```

The `theme-${rdbms.id}` class lands directly on **this card**, not a page wrapper — because the
landing page shows all three accent colors (blue/amber/cyan) side by side at once. That's different
from the hub/phase pages (§14), where only one RDBMS is ever in scope, so the theme class wraps the
whole page there instead.

### `frontend/src/components/PhaseCard.jsx`

```jsx
import { Link } from "react-router-dom";

export function PhaseCard({ rdbmsId, phaseKey, phase, index }) {
  return (
    <div className="card">
      <h3>{index}. {phase.title}</h3>
      <p className="card-endpoints">{phase.actions.map((a) => a.key).join(" · ")}</p>
      <div className="spacer" />
      <Link className="btn btn-primary" to={`/${rdbmsId}/${phaseKey}`}>Open &gt;</Link>
    </div>
  );
}
```

### `frontend/src/components/PhaseNavTabs.jsx`

```jsx
import { NavLink } from "react-router-dom";
import { PHASE_ORDER } from "../config/rdbms";

const LABELS = {
  "build-ag": "Build AG",
  "rewind-teardown": "Rewind / Teardown",
  "dr-failover": "DR Failover",
  "dr-sync": "DR Sync",
};

export function PhaseNavTabs({ rdbmsId }) {
  return (
    <div className="tabs">
      {PHASE_ORDER.map((phase) => (
        <NavLink
          key={phase}
          to={`/${rdbmsId}/${phase}`}
          className={({ isActive }) => `tab${isActive ? " active" : ""}`}
        >
          {LABELS[phase]}
        </NavLink>
      ))}
    </div>
  );
}
```

`NavLink` (not `Link`) computes `isActive` by comparing its own `to` against the current URL, so the
active tab underline (`.tab.active`, from `theme.css`) needs no manual state — it's derived purely
from the route.

---

## 14. Pages — where everything assembles

Three page components cover all 16 screens: `LandingPage` (1), `HubPage` (×3, one per RDBMS), and
`PhasePage` (×12 — 4 phases × 3 RDBMS, all from one component).

### `frontend/src/pages/LandingPage.jsx`

```jsx
import { TopBar } from "../components/TopBar";
import { RdbmsCard } from "../components/RdbmsCard";
import { RDBMS } from "../config/rdbms";

export function LandingPage() {
  return (
    <>
      <TopBar />
      <div className="container">
        <h1 className="page-title">Choose a database platform</h1>
        <p className="page-sub">Pick a platform to manage its Always On / Data Guard / replication lifecycle.</p>
        <div className="card-grid">
          {RDBMS.map((r) => <RdbmsCard key={r.id} rdbms={r} />)}
        </div>
      </div>
    </>
  );
}
```

### `frontend/src/pages/HubPage.jsx`

```jsx
import { useParams, Navigate } from "react-router-dom";
import { TopBar } from "../components/TopBar";
import { PhaseCard } from "../components/PhaseCard";
import { HostsPanel } from "../components/HostsPanel";
import { HistoryTable } from "../components/HistoryTable";
import { ComingSoonBanner } from "../components/ComingSoonBanner";
import { getRdbms, PHASE_ORDER } from "../config/rdbms";

export function HubPage() {
  const { rdbms: rdbmsId } = useParams();
  const rdbms = getRdbms(rdbmsId);
  if (!rdbms) return <Navigate to="/" replace />;

  return (
    <div className={`theme-${rdbms.id}`}>
      <TopBar />
      <div className="container">
        <a className="back-link" href="/">&larr; Back</a>
        <h1 className="page-title">{rdbms.label} — {rdbms.subtitle}</h1>
        {!rdbms.live && <ComingSoonBanner docPath={rdbms.docHref} />}

        <div className="status-bar">
          {rdbms.hosts.map((h) => (
            <span key={h.key}>
              <strong>{h.label}</strong>{" "}
              <span className={`badge ${rdbms.live ? "badge-live" : "badge-soon"}`}>
                {rdbms.live ? "up" : "n/a"}
              </span>
            </span>
          ))}
        </div>

        <div className="card-grid">
          {PHASE_ORDER.map((key, i) => (
            <PhaseCard key={key} rdbmsId={rdbms.id} phaseKey={key} phase={rdbms.phases[key]} index={i + 1} />
          ))}
        </div>

        <div style={{ marginTop: 24 }}>
          <HostsPanel apiBase={rdbms.apiBase} live={rdbms.live} />
          <HistoryTable apiBase={rdbms.apiBase} live={rdbms.live} />
        </div>
      </div>
    </div>
  );
}
```

`useParams()` reads `:rdbms` straight from the URL (`/mssql` → `rdbmsId = "mssql"`); `getRdbms`
(from `config/rdbms.js`) looks it up. An unknown id (`/postgres`, a typo) redirects home instead of
rendering a broken page.

### `frontend/src/pages/PhasePage.jsx`

This is the component doing the most work in the whole app — one template rendering all 4 phases
for all 3 RDBMS, entirely driven by `config/rdbms.js`.

```jsx
import { useState } from "react";
import { useParams, Navigate } from "react-router-dom";
import { TopBar } from "../components/TopBar";
import { PhaseNavTabs } from "../components/PhaseNavTabs";
import { ActionCard } from "../components/ActionCard";
import { TaskProgressPanel } from "../components/TaskProgressPanel";
import { LogTailPane } from "../components/LogTailPane";
import { ComingSoonBanner } from "../components/ComingSoonBanner";
import { RewindPlanAccordion } from "../components/RewindPlanAccordion";
import { getRdbms } from "../config/rdbms";

export function PhasePage({ phaseKey }) {
  const { rdbms: rdbmsId } = useParams();
  const rdbms = getRdbms(rdbmsId);
  const [activeTaskId, setActiveTaskId] = useState(null);
  const [runningStatus, setRunningStatus] = useState("idle");

  if (!rdbms) return <Navigate to="/" replace />;
  const phase = rdbms.phases[phaseKey];
  const isBusy = runningStatus === "running" || runningStatus === "queued";

  return (
    <div className={`theme-${rdbms.id}`}>
      <TopBar />
      <div className="container">
        <a className="back-link" href={`/${rdbms.id}`}>&larr; {rdbms.label} hub</a>
        <PhaseNavTabs rdbmsId={rdbms.id} />

        {!rdbms.live && <ComingSoonBanner docPath={rdbms.docHref} />}
        {phase.showPlan && <RewindPlanAccordion apiBase={rdbms.apiBase} live={rdbms.live} />}

        {phase.actions.map((action) => (
          <ActionCard
            key={action.key}
            action={action}
            apiBase={rdbms.apiBase}
            live={rdbms.live}
            disabled={!rdbms.live || isBusy}
            onStarted={setActiveTaskId}
          />
        ))}

        {rdbms.live && (
          <>
            <TaskProgressPanel apiBase={rdbms.apiBase} taskId={activeTaskId} onStatusChange={setRunningStatus} />
            <LogTailPane apiBase={rdbms.apiBase} taskId={activeTaskId} />
          </>
        )}
      </div>
    </div>
  );
}
```

**Why one component instead of four page files** (a `BuildAgPage.jsx`, `RewindTeardownPage.jsx`,
etc.): the only thing that actually differs between the 4 phase screens is *which* config object
they read (`phase = rdbms.phases[phaseKey]`) and one boolean (`phase.showPlan`, only true for
Rewind/Teardown). Four separate files would mean four near-identical copies of this same JSX with
one line different — the config-driven approach the design doc calls for (§2 of
`frontend-ui-design.md`: "one parameterized page template") is what keeps that from happening.

**`isBusy` and why it's state, not a second poll:** `PhasePage` doesn't poll `/status` itself — it
receives status updates via `TaskProgressPanel`'s `onStatusChange` callback (§11) and stores the
latest value in `runningStatus`. When that becomes `"running"` or `"queued"`, `isBusy` flips true,
which flows into every `ActionCard`'s `disabled` prop — so clicking "Backup" while "Install" is still
running is impossible, matching the design doc's stated rule ("only one playbook run in flight").
This is *lifting state up*: `TaskProgressPanel` is the only component that actually talks to
`/status`; everyone else who needs to know "is something running" reacts to what it reports, rather
than each polling independently.

### `frontend/src/App.jsx`

```jsx
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { LandingPage } from "./pages/LandingPage";
import { HubPage } from "./pages/HubPage";
import { PhasePage } from "./pages/PhasePage";

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/:rdbms" element={<HubPage />} />
        <Route path="/:rdbms/build-ag" element={<PhasePage phaseKey="build-ag" />} />
        <Route path="/:rdbms/rewind-teardown" element={<PhasePage phaseKey="rewind-teardown" />} />
        <Route path="/:rdbms/dr-failover" element={<PhasePage phaseKey="dr-failover" />} />
        <Route path="/:rdbms/dr-sync" element={<PhasePage phaseKey="dr-sync" />} />
      </Routes>
    </BrowserRouter>
  );
}
```

Each phase route passes a different literal `phaseKey` prop into the *same* `PhasePage` component —
this is the one place the 4 phases are spelled out explicitly, matching the site map in
`frontend-ui-design.md` §2 exactly.

### `frontend/src/main.jsx`

Replace the generated file with:

```jsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>
);
```

One dev-only quirk worth knowing about: `StrictMode` intentionally double-invokes effects on mount
in development (not in production builds) to help surface bugs. You may see `usePolling`'s effect
fire, clean up, and fire again once when a page first loads — that's expected, self-corrects
immediately, and isn't something to "fix."

---

## 15. Running it end to end

**Terminal 1 — the backend** (per CLAUDE.md's documented command, from the repo root):

```bash
cd python-fastapi-mssql
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — the frontend:**

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173`. You should see the landing page with three cards — MSSQL glowing
blue and marked `ONLINE`, Oracle amber and MySQL cyan, both marked `COMING SOON` and unclickable
into anything but a preview. Click into MSSQL → Build AG, click **Check AG Status** (harmless,
read-only) — within ~2 seconds the response panel should fill in with a real `task_id`, and Task
Progress/Log Tail below should start reflecting the real Ansible run.

---

## 16. How it all flows together

Walking through one click end to end — clicking **Form Always On AG** on `/mssql/build-ag`:

```
PhasePage(phaseKey="build-ag")
  └─ looks up rdbms = getRdbms("mssql"), phase = rdbms.phases["build-ag"]
  └─ renders 8 <ActionCard>, one per phase.actions entry

ActionCard(action=alwayson) — user clicks "Form Always On AG ▷"
  └─ run() → apiRequest("/api/v1/deploy", "/alwayson", {method:"POST"})
       └─ fetch("/api/v1/deploy/alwayson", {method:"POST"})   ← relative URL
            └─ Vite dev server: matches proxy rule "/api" → forwards to
               http://<VITE_DEV_PROXY_TARGET>/api/v1/deploy/alwayson
                 └─ FastAPI: deploy_alwayson() in app/routes/deploy.py
                      └─ deployer.start_task("alwayson") → task_id
                      └─ background_tasks.add_task(...)   ← returns immediately,
                                                              playbook runs in background
                      └─ 200 OK  {status:"initiated", task_id, message, playbook, operations, ...}
  └─ setResponse(json)        → this card's own <ResponseSummary> fills in
  └─ onStarted(json.task_id)  → bubbles up to PhasePage

PhasePage
  └─ setActiveTaskId(task_id)
  └─ passes taskId down to <TaskProgressPanel> and <LogTailPane>

TaskProgressPanel                                    LogTailPane
  └─ usePolling(GET /status, every 2s)                └─ usePolling(GET /history, every 2s)
       └─ latest_task.status: queued → running           └─ finds executions[] entry by task_id
       └─ onStatusChange("running")                       └─ results.stdout still null → "waiting…"
            └─ PhasePage: setRunningStatus("running")
                 └─ isBusy = true
                      └─ every other <ActionCard>'s `disabled` prop flips true

  ... ~30 min later, alwayson.yml finishes ...

TaskProgressPanel                                    LogTailPane
  └─ latest_task.status: "success"                    └─ entry.results.stdout populated
       └─ stopWhen() true → polling stops                  └─ stopWhen() true → polling stops
       └─ onStatusChange("success")                         └─ renders last 40 lines of Ansible output
            └─ isBusy = false → cards re-enable
```

Every other button on every other phase page follows this exact same path — the only things that
change are which `action` object from `config/rdbms.js` gets passed to `ActionCard`, and, for
`failover`/`sync-rebuild`, that `params` get sent as query string values instead of nothing.

---

## 17. Troubleshooting

- **Network error / nothing happens on click, console shows a failed fetch to `/api/...`** — the
  FastAPI process isn't running, or `VITE_DEV_PROXY_TARGET` in `.env` points at the wrong host/port.
  Confirm with `curl http://<target>:8000/api/v1/deploy/status` from the same machine running Vite.
- **CORS error in the browser console** — this means something bypassed the proxy (e.g. `apiRequest`
  was changed to fetch an absolute URL). Every call should hit a **relative** path starting with
  `/api/...`; only Vite's dev server should ever see an absolute FastAPI URL (§4).
- **A card's Response always says "Not run yet in this session"** — check `rdbms.live` for that
  RDBMS in `config/rdbms.js`; Oracle/MySQL are `live: false` by design (§6) until real backends
  exist.
- **Confirm dialog won't let you submit Teardown/Rewind/Reset Baseline** — you have to type the
  exact `confirmText` from the action's config entry (`"TEARDOWN"`, `"REWIND"`, `"RESET"`).
- **Log Tail never shows output even after the task succeeds** — check the FastAPI process's own
  stdout/`python-fastapi-mssql/logs/ansible.log` for the actual playbook error; `LogTailPane` can
  only show what `GET /history` returns, and a task can complete with `status: "failed"` and a
  populated `error` field instead of `stdout` if the playbook itself errored.

---

## 18. Where Oracle and MySQL slot in later

This is the payoff of the config-driven design (§6): once `python-fastapi-oracle-dg/` or
`python-fastapi-mysql/` exists as a real FastAPI service (per their own `*-fastapi-build-from-scratch.md`
guides in this folder), turning their screens live is a **data change**, not new components:

1. Flip `live: false` → `live: true` on that RDBMS's entry in `config/rdbms.js`.
2. Set `apiBase` to that service's real base path (e.g. `/api/oracle/deploy`).
3. If it runs on a different host/port than the MSSQL service, add a second proxy rule in
   `vite.config.js` (`"/api/oracle": { target: ..., changeOrigin: true }`).
4. Double-check each action's `path`/`params` against that service's actual route signatures —
   the entries here were written from the *design* docs (`docs/guides/oracle-*-implementation.md`),
   so confirm the real implementation matches before trusting them.

No `ActionCard`, `PhasePage`, `TaskProgressPanel`, or any other component needs to change — they
were already built generic enough to not know which RDBMS they're rendering.
