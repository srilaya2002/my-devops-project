# SSO / OAuth2 / OIDC — Design

> Second of three learning docs — [`sso-oauth-brainstorm.md`](sso-oauth-brainstorm.md)
> (read first if you haven't) surveyed the landscape; this doc commits to a
> stack and an architecture and explains every "why." The stage-by-stage
> code lives in [`sso-oauth-implementation.md`](sso-oauth-implementation.md).
>
> **Decisions locked in by this doc:** Keycloak as the Authorization
> Server, Authorization Code + PKCE as the login flow, local JWT validation
> via JWKS (not per-request introspection) in FastAPI, two realm roles
> (`mssql-viewer`, `mssql-admin`) mapped onto the existing `deploy.py`
> routes, Keycloak co-located on `devops_VM1` in its own Docker container.

## Design thinking

**Why Keycloak over Authentik/ORY/Casdoor.** All four would teach OIDC
fundamentals equally well. Keycloak wins here specifically because it's the
most likely thing you'll meet at an actual employer running self-hosted
identity — the "realm / client / role mapper / JWKS" vocabulary and the
admin console's shape are close to industry-default. This is a deliberate
choice to optimize for transferable knowledge over the smoothest
onboarding experience.

**Why Authorization Code + PKCE, not Implicit or Password grants.**
- The **Implicit flow** returns the access token directly in the browser's
  URL fragment with no code-exchange step — it's deprecated in the current
  OAuth2 Security Best Current Practice specifically because tokens end up
  in browser history, referrer headers, and server logs. No reason to learn
  it as anything but "here's what not to do."
- The **Resource Owner Password Credentials (ROPC) grant** — the client
  collects the user's actual username/password and trades them directly for
  a token — defeats the entire point of SSO (the client sees the password)
  and is being removed from the OAuth2.1 draft spec entirely. Skip it.
- **Authorization Code + PKCE** is the only flow OAuth2.1 recommends for
  browser SPAs. The user authenticates *on Keycloak's own login page*, never
  typing a password into your React app at all — that's the actual point of
  SSO. PKCE (a client-generated `code_verifier`/`code_challenge` pair) closes
  the one gap Authorization Code has for a *public* client (one that can't
  hold a secret): without PKCE, a malicious app on the same device could
  intercept the redirect's auth code and trade it for tokens itself. With
  PKCE, that theft is useless without the original random verifier, which
  never leaves the legitimate SPA's memory.

**Why FastAPI validates JWTs locally via JWKS instead of calling Keycloak's
introspection endpoint on every request.** Two real options exist:
- **Token introspection** (`POST /realms/.../protocol/openid-connect/token/introspect`) —
  the Resource Server asks the AS "is this token still valid?" on every
  single API call. Pro: instant revocation (log a user out server-side and
  their very next API call fails). Con: every API request now has a network
  round-trip to Keycloak in the critical path, and Keycloak becomes a
  single point of failure for *every* request, not just logins.
- **Local JWT validation via JWKS** — FastAPI fetches Keycloak's public
  signing keys once (cached, refreshed occasionally) and verifies the JWT's
  signature and claims (`exp`, `iss`, `aud`) itself, no network call per
  request. Con: a token stays valid until it expires even if the user is
  deleted/disabled in the meantime (mitigated by short access-token
  lifetimes — the design below uses 5 minutes).

This design uses **local JWKS validation**, because the performance/
architecture lesson (how a Resource Server can trust tokens without
constantly phoning home) is one of the most important, and most commonly
misunderstood, ideas in this whole space. Introspection is worth trying
later as a deliberate follow-up experiment, not the default here.

**Why two roles, not a role-per-endpoint.** `mssql-viewer` (read-only:
`ag-status`, `/history`, `/hosts`, `/ping`) and `mssql-admin` (everything
that mutates the AG or the VMs: `/failover`, `/sync-rebuild`, `/alwayson`,
`/full-ag`, `/teardown`, `/rewind`, `/build`, `/install*`) mirrors the real
shape of who should be allowed to do what in this lab — "can look" vs "can
break things" — without over-engineering a permission system nobody needs
for a two-person home lab. A `require_role()` dependency factory (see
implementation doc) makes adding a third role later a one-line change per
route, so this isn't a dead end if you outgrow it.

**Why Keycloak runs on `devops_VM1` in its own container, not a new VM.**
`devops_VM1` already doubles as the FastAPI/Ansible controller — this
mirrors that existing "VM1 wears multiple hats" pattern from the main
project rather than introducing lab topology sprawl for a learning
exercise. Keycloak needs its own Postgres database too (its default H2 dev
database isn't meant to survive a restart) — both run as sibling containers
via a small `docker-compose.yml`, entirely separate from the FastAPI venv
and from MSSQL, so nothing here can break the DR automation work.

**Why plain HTTP is acceptable here, with one explicit caveat.** OAuth2/
OIDC's redirect-based login flow works fine over HTTP for local, single-user
learning — Keycloak's dev mode and `react-oidc-context` both support it
without complaint. The caveat to internalize, not skip: bearer tokens
crossing plaintext HTTP is a real vulnerability in anything beyond a lab —
anyone who can see the traffic can replay the token until it expires. Worth
saying out loud once here rather than silently normalizing HTTP everywhere
and forgetting TLS is a hard requirement the moment this leaves a single
trusted machine.

## Architecture

```
                          devops_VM1 (192.168.70.129)
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                        │
   │   ┌───────────────────────┐        ┌────────────────────────────┐     │
   │   │  FastAPI :8000          │        │  Docker: keycloak :8080      │     │
   │   │  (existing venv,         │        │   realm: devops-lab           │     │
   │   │   uvicorn --reload)      │◄──────►│   client: mssql-react-spa     │     │
   │   │                          │  JWKS  │   (public, PKCE required)     │     │
   │   │  NEW: app/auth.py        │  fetch │   roles: mssql-viewer,        │     │
   │   │  (JWT verify + role      │        │          mssql-admin          │     │
   │   │   dependency, wired      │        │                              │     │
   │   │   into routes/deploy.py) │        ├────────────────────────────┤     │
   │   │                          │        │  Docker: keycloak-db :5432    │     │
   │   └───────────┬──────────────┘        │   postgres, keycloak's        │     │
   │               │                        │   own persistent storage      │     │
   │               │ proxied in dev via     └────────────────────────────┘     │
   │               │ vite.config.js's                                            │
   │               │ /api/* -> :8000                                             │
   │   ┌───────────▼──────────────┐                                              │
   │   │  React SPA (Vite dev       │  Direct browser -> Keycloak for the login   │
   │   │  server :5173, or a         │  redirect -- NOT proxied through FastAPI;  │
   │   │  built bundle later)        │  the browser talks to :8080 directly.      │
   │   │                             │                                            │
   │   │  NEW: src/auth/ (oidc       │                                            │
   │   │  config, AuthProvider,      │                                            │
   │   │  ProtectedRoute)            │                                            │
   │   └──────────────┬──────────────┘                                          │
   │                  │                                                          │
   └──────────────────┼──────────────────────────────────────────────────────────┘
                       │
                 Browser (your workstation)
```

Everything stays on `devops_VM1` for this learning setup — no new VM, no
change to `devops_VM2` or the MSSQL AG topology at all.

## Sequence diagrams

**Login (Authorization Code + PKCE):**
```
Browser              React SPA                Keycloak              FastAPI
  │                      │                        │                    │
  │  open app            │                        │                    │
  ├─────────────────────►│                        │                    │
  │                      │ no valid session ->     │                    │
  │                      │ generate code_verifier   │                    │
  │                      │ + code_challenge (S256)  │                    │
  │  redirect to          │                        │                    │
  │  /realms/devops-lab/  │                        │                    │
  │  protocol/openid-     │                        │                    │
  │  connect/auth?...      │                        │                    │
  │◄─────────────────────┤                        │                    │
  │  GET .../auth (code_challenge attached)          │                    │
  ├──────────────────────────────────────────────►│                    │
  │  Keycloak's own login page                       │                    │
  │◄──────────────────────────────────────────────┤                    │
  │  user enters username/password *on Keycloak*     │                    │
  ├──────────────────────────────────────────────►│                    │
  │  redirect back to SPA with ?code=...              │                    │
  │◄──────────────────────────────────────────────┤                    │
  │  GET /callback?code=... │                        │                    │
  ├─────────────────────►│                        │                    │
  │                      │  POST .../token            │                    │
  │                      │  code + code_verifier       │                    │
  │                      ├───────────────────────►│                    │
  │                      │  access_token, id_token,    │                    │
  │                      │  refresh_token (JSON)        │                    │
  │                      │◄───────────────────────┤                    │
  │  app shows logged-in state                      │                    │
  │◄─────────────────────┤                        │                    │
```

**Calling a protected API:**
```
React SPA                                    FastAPI                Keycloak
   │  GET /api/v1/deploy/ag-status                │                     │
   │  Authorization: Bearer <access_token>          │                     │
   ├──────────────────────────────────────────────►│                     │
   │                                                │  first call only:   │
   │                                                │  GET JWKS, cache it │
   │                                                ├────────────────────►│
   │                                                │◄────────────────────┤
   │                                                │  verify signature,   │
   │                                                │  check exp/iss/aud,  │
   │                                                │  read realm_access   │
   │                                                │  .roles claim        │
   │  200 OK (role allowed) or 401/403                │                     │
   │◄──────────────────────────────────────────────┤                     │
```

**Silent token refresh** (access token about to expire, user still has an
active session): `react-oidc-context` runs this automatically in a hidden
iframe / via the refresh token, before the access token's 5-minute lifetime
runs out, so the user never notices. Worth watching happen once in the
Network tab during implementation, specifically to *see* it rather than
just take it on faith.

## Claims / role mapping

| Realm role | Routes allowed | Rationale |
| --- | --- | --- |
| `mssql-viewer` | `GET /hosts`, `GET /history`, `POST /ping`, `POST /ag-status` | Read-only / non-mutating — safe for anyone to run at any time, matches how `ag_status.yml` is already documented as "safe to run any time" in the DR guides. |
| `mssql-admin` | Everything `mssql-viewer` can do, **plus** `/install*`, `/build`, `/backup`, `/restore-db`, `/alwayson`, `/full-ag`, `/failover`, `/sync-rebuild`, `/teardown`, `/rewind`, `/reset-baseline` | Anything that mutates SQL Server state, the AG, or the VMs' filesystem. |

Roles are read from the JWT's `realm_access.roles` claim (Keycloak's
default location for realm-level roles) — the implementation doc's
`require_role()` dependency checks for membership in that list.

## Security considerations

- **Token storage in the browser.** `react-oidc-context` (built on
  `oidc-client-ts`) defaults to `sessionStorage`. That's the right call for
  this learning setup: survives a page refresh, cleared when the tab
  closes, and — critically — not shared across tabs the way `localStorage`
  would be, which limits blast radius if XSS ever creeps into the SPA.
  `localStorage` is worth trying once as a deliberate "see the difference"
  experiment, not as the default.
- **CORS.** FastAPI's dev server and Vite's dev server run on different
  ports (`8000` vs `5173`), but the Vite proxy (already documented in
  `react-frontend-build-from-scratch.md`) means the *browser* only ever
  talks to `5173` for `/api/*` — no CORS headers needed for that path. The
  browser *does* talk directly to Keycloak on `8080` for the login
  redirect, which is a plain top-level navigation, not a fetch/XHR — also no
  CORS involved. CORS only becomes a real question once the frontend is a
  built bundle served from somewhere other than where Vite's proxy exists —
  worth flagging now, solving later.
- **Redirect URI allowlist.** Keycloak's client config only accepts exact
  registered redirect URIs (`http://localhost:5173/callback` etc.) — this is
  the mechanism that stops a malicious site from registering itself as a
  valid redirect target for your client ID. Never widen this to a wildcard
  for anything beyond a single-developer lab.
- **Access token lifetime.** 5 minutes (Keycloak realm default is longer;
  this design shortens it deliberately) — short enough that a leaked token
  is a narrow window, long enough that silent refresh isn't firing
  constantly. Refresh tokens default to 30 minutes idle / longer max in this
  design — tune later once you've felt what the defaults feel like to use.
- **`aud` (audience) claim.** FastAPI's validation must check the token's
  `aud` claim matches the expected client ID, not just that the signature
  is valid — otherwise a token issued for some *other* client in the same
  Keycloak realm would also pass validation. Called out explicitly here
  because it's the single most common real-world JWT-validation bug.

## Prerequisites

- Docker + Docker Compose available on `devops_VM1` (separate from the
  Ansible/Python venv used by the rest of this repo).
- The existing `python-fastapi-mssql` FastAPI app running (per
  `mssql-fastapi-build-from-scratch.md`).
- The React frontend scaffolded per `react-frontend-build-from-scratch.md`
  (or at least `frontend/` existing with Vite configured) — this design
  assumes that guide's directory layout and `api/client.js` pattern.

## Phased implementation plan (detailed in the implementation doc)

1. Stand up Keycloak + its Postgres via Docker Compose.
2. Configure the `devops-lab` realm, the `mssql-react-spa` public client
   (PKCE required, redirect URIs), the two realm roles, and a test user per
   role.
3. FastAPI: add `app/auth.py` (JWKS fetch/cache, JWT verification,
   `get_current_user`, `require_role()`), wire into `routes/deploy.py`.
4. React: add `src/auth/` (OIDC config, `AuthProvider`, login/logout UI,
   `ProtectedRoute`), attach bearer tokens in `api/client.js`.
5. Verify: browser login round-trip, 401 with no token, 403 with wrong
   role, 200 with the right role.

## What this design doesn't cover

Worth knowing as you build this, not because it needs fixing right now:
- **No MFA.** Password-only login for this learning setup. Keycloak
  supports TOTP MFA out of the box — a good follow-up exercise once the
  basic flow works.
- **No user self-registration flow.** Test users are created manually via
  the Keycloak admin console — fine for two people in a home lab.
- **No production TLS anywhere in this design.** Explicitly a lab-only
  setup per the HTTP caveat above.
- **No refresh-token rotation hardening or logout-everywhere.** Keycloak
  supports both; out of scope for a first pass at "does OIDC login work at
  all."
- **No SAML.** Keycloak supports it, but OIDC is the more broadly relevant
  thing to learn first given how much of the modern web uses it.
