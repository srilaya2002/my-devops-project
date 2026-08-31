# SSO / OAuth2 / OIDC — Implementation Guide

> Third of three learning docs. Builds exactly what
> [`sso-oauth-design.md`](sso-oauth-design.md) specified: Keycloak (OSS IdP)
> + Authorization Code with PKCE + local JWT validation in FastAPI via JWKS
> + two realm roles mapped onto `routes/deploy.py`. If something here
> doesn't match what you see on screen, the design doc's "Design thinking"
> section explains the *why* behind each choice — check there before
> assuming the code is wrong.

## Prerequisites

- `python-fastapi-mssql`'s FastAPI app running per
  `mssql-fastapi-build-from-scratch.md`.
- `frontend/` scaffolded per `react-frontend-build-from-scratch.md` (Vite +
  React + `react-router-dom`, the `apiRequest` client in
  `src/api/client.js`, the proxy in `vite.config.js`).
- Docker + Docker Compose on `devops_VM1`, separate from the Python venv.

## Stage-by-stage

### 1. Keycloak + Postgres — `docker-compose.auth.yml` (repo root or `python-fastapi-mssql/`)

```yaml
services:
  keycloak-db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: keycloak
      POSTGRES_USER: keycloak
      POSTGRES_PASSWORD: keycloak_dev_password
    volumes:
      - keycloak_db_data:/var/lib/postgresql/data

  keycloak:
    image: quay.io/keycloak/keycloak:25.0
    restart: unless-stopped
    command: start-dev
    environment:
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://keycloak-db:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: keycloak_dev_password
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: admin_dev_password
      KC_HOSTNAME_STRICT: "false"
      KC_HTTP_ENABLED: "true"
    ports:
      - "8080:8080"
    depends_on:
      - keycloak-db

volumes:
  keycloak_db_data:
```

`start-dev` is Keycloak's development mode — HTTP allowed, no need for a
real certificate, matches the design doc's "plain HTTP is acceptable here"
call. Bring it up:

```bash
docker compose -f docker-compose.auth.yml up -d
# wait ~30-60s for Keycloak's first boot, then:
curl -s http://localhost:8080/health/ready
```

Admin console: `http://192.168.70.129:8080` (or `localhost:8080` if you're
on VM1 itself), login `admin` / `admin_dev_password`.

### 2. Configure the realm, client, roles, and test users

Everything below can be done through the admin console UI (Keycloak's
left-hand menu: **Realm settings**, **Clients**, **Realm roles**, **Users**)
— worth doing it by hand once to see where each concept lives before ever
scripting it.

**Realm:** top-left realm dropdown → **Create realm** → name `devops-lab`.

**Client:** inside `devops-lab` → **Clients** → **Create client**:
- Client ID: `mssql-react-spa`
- Client authentication: **Off** (this makes it a *public* client — a
  browser SPA can't hold a secret, so it doesn't get one; PKCE is what
  makes this safe instead — see design doc)
- Standard flow (Authorization Code): **On**
- Direct access grants: **Off** (this disables the ROPC grant the design
  doc ruled out — leave it off deliberately)
- Valid redirect URIs: `http://localhost:5173/callback`
- Valid post logout redirect URIs: `http://localhost:5173`
- Web origins: `http://localhost:5173`

Under the client's **Advanced** tab, confirm **Proof Key for Code Exchange
Code Challenge Method** is set to `S256` — this is what makes PKCE
mandatory for this client, not optional.

**Realm roles:** **Realm roles** → **Create role** → `mssql-viewer`. Repeat
for `mssql-admin`.

**Test users:** **Users** → **Add user** → username `viewer1`, email
whatever → **Create**, then **Credentials** tab → set a password, toggle
**Temporary** off → **Role mapping** tab → assign `mssql-viewer`. Repeat for
`admin1` with `mssql-admin` (and also assign it `mssql-viewer`, since
"admin" here means "can also do everything viewer can").

**Sanity check** — confirm the realm's OIDC discovery document is live:
```bash
curl -s http://localhost:8080/realms/devops-lab/.well-known/openid-configuration | python3 -m json.tool | head -20
```
You should see `authorization_endpoint`, `token_endpoint`, and
`jwks_uri` — the three URLs everything below is built on.

### 3. FastAPI — new file `app/auth.py`

```python
"""JWT verification and role-based authorization against Keycloak."""

from __future__ import annotations

import time
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError

from app.config import settings

bearer_scheme = HTTPBearer()

_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}
_JWKS_CACHE_TTL_SECONDS = 300


def _get_jwks() -> dict:
    """Fetch Keycloak's signing keys, cached for 5 minutes.

    This is the entire reason FastAPI doesn't need to call Keycloak on every
    request to validate a token -- the *keys* are cached and reused; only
    the JWT's signature/claims are checked per request, locally, in-process.
    """
    now = time.time()
    if _jwks_cache["keys"] is None or (now - _jwks_cache["fetched_at"]) > _JWKS_CACHE_TTL_SECONDS:
        resp = httpx.get(settings.OIDC_JWKS_URL, timeout=5)
        resp.raise_for_status()
        _jwks_cache["keys"] = resp.json()
        _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]


def _decode_token(token: str) -> dict:
    try:
        jwks = _get_jwks()
        unverified_header = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == unverified_header["kid"]), None)
        if key is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown signing key")

        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.OIDC_AUDIENCE,
            issuer=settings.OIDC_ISSUER,
        )
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}") from exc


async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> dict:
    """FastAPI dependency: verifies the bearer token, returns its claims.

    Use directly (`user: dict = Depends(get_current_user)`) when a route
    just needs *any* authenticated user. Use `require_role()` below when a
    route needs a *specific* role.
    """
    return _decode_token(creds.credentials)


def require_role(role: str):
    """Dependency factory: returns a dependency that also checks `role`.

    Usage: `Depends(require_role("mssql-admin"))` on any route that mutates
    the AG or the VMs -- see routes/deploy.py for exactly which ones.
    """

    async def _check(user: dict = Depends(get_current_user)) -> dict:
        roles = user.get("realm_access", {}).get("roles", [])
        if role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{role}' -- you are authenticated but not authorized for this.",
            )
        return user

    return _check
```

Two details worth understanding, not just copying:
- `get_current_user` only proves the request is authenticated (**401** if
  missing/invalid). `require_role()` additionally proves the request is
  *authorized* for this specific action (**403** if authenticated but the
  wrong role) — the design doc's "done" checklist item about the 401/403
  distinction is exactly this pair of functions.
- The `aud` (audience) check inside `jwt.decode(...)` is the single most
  important line in this file — see the design doc's security section on
  why skipping it is the most common real-world JWT bug.

### 4. `app/config.py` additions

```python
class Settings(BaseSettings):
    # ... existing fields ...
    OIDC_ISSUER: str = "http://localhost:8080/realms/devops-lab"
    OIDC_JWKS_URL: str = "http://localhost:8080/realms/devops-lab/protocol/openid-connect/certs"
    OIDC_AUDIENCE: str = "mssql-react-spa"
```

Add matching lines to `.env` / `.env.example`, and adjust the host from
`localhost` to `192.168.70.129` if FastAPI and Keycloak end up on different
reachability paths for your setup.

Add the dependency: `pip install python-jose[cryptography] httpx` (add to
`requirements.txt`).

### 5. Wire it into `app/routes/deploy.py`

Read-only routes get `get_current_user` (any authenticated user); everything
that mutates gets `require_role("mssql-admin")`. Example for two routes —
apply the same pattern to the rest per the design doc's table:

```python
from app.auth import get_current_user, require_role

@router.post("/ag-status")
async def deploy_ag_status(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    logger.info(f"Received deployment request - AG status (user: {user.get('preferred_username')})")
    ...


@router.post("/failover")
async def deploy_failover(
    background_tasks: BackgroundTasks,
    target: str,
    mode: str = "planned",
    user: dict = Depends(require_role("mssql-admin")),
):
    ...
```

**Verify with `curl` before touching the frontend at all** — this isolates
backend correctness from any React/OIDC-client complexity:

```bash
# Get a token the fast way, for testing only (see "What this doesn't cover" --
# this uses the password grant directly against Keycloak from curl, which is
# fine for a terminal test but is exactly the ROPC flow the design doc ruled
# out for the *real* SPA login).
TOKEN=$(curl -s -X POST http://localhost:8080/realms/devops-lab/protocol/openid-connect/token \
  -d grant_type=password -d client_id=mssql-react-spa \
  -d username=viewer1 -d password='<the password you set>' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# No token -> 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/v1/deploy/ag-status

# viewer1's token on a viewer-allowed route -> 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/v1/deploy/ag-status \
  -H "Authorization: Bearer $TOKEN"

# viewer1's token on an admin-only route -> 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://localhost:8000/api/v1/deploy/failover?target=vm2" \
  -H "Authorization: Bearer $TOKEN"
```

If the client's **Direct access grants** toggle from step 2 is off (as
instructed), that first `curl` will fail — that's correct/expected; flip it
on *temporarily* just to mint a test token this way, then back off
afterward, or mint a token through the real browser flow in step 7 instead
and paste it in.

### 6. React — install and configure the OIDC client

```bash
cd frontend
npm install react-oidc-context oidc-client-ts
```

`frontend/src/auth/oidcConfig.js`:
```js
export const oidcConfig = {
  authority: "http://localhost:8080/realms/devops-lab",
  client_id: "mssql-react-spa",
  redirect_uri: "http://localhost:5173/callback",
  post_logout_redirect_uri: "http://localhost:5173",
  scope: "openid profile email",
  response_type: "code", // Authorization Code flow; PKCE is automatic in oidc-client-ts
};
```

`frontend/src/main.jsx` — wrap `App` in `AuthProvider`:
```jsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider } from "react-oidc-context";
import { App } from "./App";
import { oidcConfig } from "./auth/oidcConfig";
import "./index.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <AuthProvider {...oidcConfig}>
      <App />
    </AuthProvider>
  </StrictMode>
);
```

`frontend/src/auth/ProtectedRoute.jsx` — gate a route behind login:
```jsx
import { useAuth } from "react-oidc-context";

export function ProtectedRoute({ children }) {
  const auth = useAuth();

  if (auth.isLoading) return <p>Loading...</p>;
  if (!auth.isAuthenticated) {
    auth.signinRedirect();
    return null;
  }
  return children;
}
```

`frontend/src/components/AuthStatus.jsx` — login/logout UI, drop this into
the existing `TopBar.jsx`:
```jsx
import { useAuth } from "react-oidc-context";

export function AuthStatus() {
  const auth = useAuth();

  if (auth.isLoading) return null;
  if (!auth.isAuthenticated) {
    return <button onClick={() => auth.signinRedirect()}>Log in</button>;
  }
  return (
    <span>
      {auth.user?.profile.preferred_username}
      <button onClick={() => auth.signoutRedirect()}>Log out</button>
    </span>
  );
}
```

### 7. Attach the bearer token to every API call — `frontend/src/api/client.js`

Extend the existing `apiRequest` (from `react-frontend-build-from-scratch.md`
§7) to accept and send a token, rather than writing a second client:

```js
const DEFAULT_TIMEOUT_MS = 15000;

export async function apiRequest(base, path, { method = "GET", params, timeoutMs = DEFAULT_TIMEOUT_MS, token } = {}) {
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
      headers: token ? { Authorization: `Bearer ${token}` } : {},
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

Every call site now needs `auth.user?.access_token` passed through as
`token` — e.g. inside `ActionCard.jsx` or wherever `apiRequest` is currently
called, pull `const auth = useAuth()` and pass `{ token: auth.user?.access_token }`.

### 8. Add the `/callback` route — `frontend/src/App.jsx`

`react-oidc-context` needs a route to land on after Keycloak redirects
back with the auth code:

```jsx
import { useAuth } from "react-oidc-context";
import { useNavigate } from "react-router-dom";
import { useEffect } from "react";

function Callback() {
  const auth = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (auth.isAuthenticated) navigate("/");
  }, [auth.isAuthenticated, navigate]);

  return <p>Signing you in...</p>;
}
```

Add `<Route path="/callback" element={<Callback />} />` alongside the
existing routes in `App.jsx`, and wrap whichever routes should require
login in `<ProtectedRoute>...</ProtectedRoute>`.

## Run it — full browser round-trip

```bash
# terminal 1
docker compose -f docker-compose.auth.yml up -d
# terminal 2
cd python-fastapi-mssql && source .venv/bin/activate && uvicorn app.main:app --reload --port 8000
# terminal 3
cd frontend && npm run dev
```

1. Open `http://localhost:5173`. Click **Log in** — you should land on
   **Keycloak's** login page (URL bar shows `localhost:8080`, not your
   app), not a form rendered by your own React code. That's the whole
   point of SSO — confirm it explicitly, don't skim past it.
2. Log in as `viewer1`. You should be redirected back to `/callback`, then
   to `/`, with the username showing in the top bar.
3. Open browser devtools → Network tab, trigger `ag-status` from the UI —
   confirm the request carries `Authorization: Bearer eyJ...`.
4. Try to trigger `failover` as `viewer1` from the UI (or via the same
   `curl` pattern from step 5) — confirm it's rejected with 403, and that
   the UI surfaces that error rather than silently failing.
5. Log out, log back in as `admin1` — confirm `failover` now succeeds
   (use `mode=planned` against a target you know is safe to no-op, or just
   confirm the 200 without actually letting `failover.yml` run against real
   VMs if you'd rather not touch the AG mid-experiment).

## Verification checklist

- [ ] `curl .../.well-known/openid-configuration` returns Keycloak's
      discovery document.
- [ ] No token → `401` from FastAPI.
- [ ] `viewer1` token → `200` on `ag-status`, `403` on `failover`.
- [ ] `admin1` token → `200` on both.
- [ ] Browser login lands on Keycloak's own login page, not a custom form.
- [ ] Silent refresh observed in the Network tab before the 5-minute access
      token expiry (leave the tab open and watch).
- [ ] Logout actually clears the session — reload after logout should
      require login again, not silently re-authenticate.

## Live-testing findings

*(Empty on purpose — this is where you log what actually happened when you
ran this for real, the same way the MSSQL DR guides accumulated real
findings from live testing. Expect at least one surprise around CORS,
redirect URI mismatches, or clock skew between containers causing `exp`
validation to fail — write down the exact error text and the fix, not just
"fixed it," so this doc stays useful the next time you touch it.)*

## What this implementation doesn't cover

- **The `curl` password-grant token-minting trick in step 5** is a
  test-only shortcut — it's the ROPC flow the design doc explicitly ruled
  out for the real app. Fine for a terminal sanity check with `Direct
  access grants` temporarily enabled; never wire the actual SPA to do this.
- **No automated tests.** Everything here is manually verified — a good
  follow-up exercise once the flow works is a `pytest` fixture that mints a
  test JWT (signed with a test key, not real Keycloak) to unit-test
  `require_role()` in isolation.
- **No CI/CD, no container hardening, no production Keycloak realm export/
  import workflow.** This is a dev-mode, single-machine learning setup
  exactly as scoped in the design doc.
- **No integration with the existing MSSQL DR endpoints beyond adding the
  `Depends()` calls** — this doesn't change `failover.yml`, `ansible/`, or
  anything below the FastAPI route layer. The DR automation itself is
  untouched; this only adds a gate in front of it.
