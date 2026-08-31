# SSO / OAuth2 / OIDC — Brainstorm

> First of three learning docs, meant to be read in order:
> **brainstorm** (this file, open-ended — survey the landscape, learn the
> vocabulary, don't commit to anything yet) →
> [`sso-oauth-design.md`](sso-oauth-design.md) (commit to a stack and an
> architecture, explain why) →
> [`sso-oauth-implementation.md`](sso-oauth-implementation.md) (stage-by-stage
> code, mirroring how the MSSQL DR guides are written).
>
> Goal: bolt real authentication/authorization onto the existing
> `python-fastapi-mssql` app (FastAPI backend + the planned React frontend
> from [`react-frontend-build-from-scratch.md`](react-frontend-build-from-scratch.md))
> using an **open-source** identity stack, purely to learn how SSO/OAuth2/OIDC
> actually work end to end. Nothing here is required for the MSSQL DR
> automation itself — it's a separate learning track layered on top of the
> same app.

## The problem, stated bluntly

Right now `python-fastapi-mssql`'s API has **zero authentication**. Every
route in `app/routes/deploy.py` — including `/failover`, `/sync-rebuild`,
`/teardown`, `/rewind` — is reachable by anyone who can reach port 8000, no
login, no token, nothing. You've spent this whole session doing careful,
deliberate AG failovers and watching a double-submit cause a real
split-brain *with a human at the keyboard being careful*. An unauthenticated
API is the same footgun with the safety off. That's the itch this whole
exercise scratches, even though the actual motivation here is learning, not
an audit finding.

## Vocabulary first — the four roles OAuth2 defines

Every OAuth2/OIDC diagram you'll ever see is a variation on this. Learn
these four boxes and every flow becomes readable:

```
┌────────────────────┐        ┌──────────────────────┐
│   Resource Owner    │        │  Authorization Server │
│   (you, the user)   │        │   (Keycloak, Auth0,   │
│                      │        │    Authentik, ...)    │
└──────────┬───────────┘        └───────────┬───────────┘
           │ logs in, grants consent          │ issues tokens
           ▼                                   ▼
┌────────────────────┐        ┌──────────────────────┐
│       Client         │◄──────│    (tokens flow to    │
│  (the React SPA)      │        the client, then get   │
│                      │        presented to the API)   │
└──────────┬───────────┘        └───────────────────────┘
           │ sends token with every API call
           ▼
┌────────────────────┐
│   Resource Server    │
│  (the FastAPI app)   │
└────────────────────┘
```

- **Resource Owner** — the human. Owns the data/actions being protected.
- **Client** — the app requesting access on the Resource Owner's behalf.
  Here: the React SPA. OAuth2 calls this the "client" even though it's
  *your* frontend, not some third party — worth internalizing early,
  it trips people up.
- **Authorization Server (AS)** — issues tokens after the Resource Owner
  authenticates and consents. This is the actual "identity provider" (IdP)
  piece: Keycloak, Authentik, ORY Hydra, Auth0, Okta, etc.
- **Resource Server** — the API that has to decide, per request, whether
  the presented token is valid and authorized for the thing being asked.
  Here: FastAPI.

**OAuth2 vs OIDC, in one sentence each:** OAuth2 is an *authorization*
framework — it answers "can this client do X on the user's behalf" via
access tokens, and says nothing about who the user actually is. OIDC
(OpenID Connect) is a thin *authentication* layer on top of OAuth2 that adds
a standardized **ID token** (a JWT with the user's identity claims) and a
`/userinfo` endpoint — it answers "who is this user." Almost every modern
"login with X" flow is OIDC, not bare OAuth2, because you almost always want
both "can they do this" and "who are they."

## Glossary (keep this open while reading the other two docs)

| Term | Meaning |
| --- | --- |
| **JWT** | JSON Web Token — a signed (and optionally encrypted) blob of claims, base64url-encoded in three dot-separated parts: `header.payload.signature`. Tokens in this stack are JWTs. |
| **Claim** | A single key/value fact inside a JWT payload — `sub` (subject/user id), `exp` (expiry), `realm_access.roles`, etc. |
| **Access token** | What the Client sends to the Resource Server to prove it's authorized. Short-lived (minutes). |
| **ID token** | OIDC-only. A JWT describing *who logged in*. The Client reads this; it is not meant to be sent to APIs. |
| **Refresh token** | Long-lived credential the Client exchanges for a new access token without re-prompting the user to log in. |
| **JWKS** | JSON Web Key Set — the AS publishes its public signing keys here (`/.well-known/jwks.json` or similar). The Resource Server fetches this to verify JWT signatures *without calling the AS on every request*. |
| **PKCE** | Proof Key for Code Exchange (say "pixy"). A mechanism that lets a public client (one that can't keep a secret — a browser SPA, a mobile app) safely use the Authorization Code flow. Mandatory for this project — see Design doc. |
| **Realm** (Keycloak term) / **Tenant** (generic) | An isolated namespace of users, clients, and roles inside the AS. |
| **Client (AS-side meaning)** | The AS's registration record for an application allowed to request tokens — has an ID, a redirect URI allowlist, and (for confidential clients) a secret. |
| **Scope** | A named bundle of permissions/claims a Client asks for — `openid profile email` is the standard OIDC trio. |
| **Bearer token** | "Whoever holds this token is authorized" — the `Authorization: Bearer <token>` header pattern. No proof of possession beyond having the string. |

## Options survey — open-source identity providers

| Option | What it is | Pros for *this* learning goal | Cons |
| --- | --- | --- | --- |
| **Keycloak** | Full-featured OSS IdP (Red Hat/CNCF), Java, admin UI, supports OIDC + OAuth2 + SAML | Industry-standard — what you learn here transfers directly to real jobs; huge docs/community; one Docker container gets you a realistic enterprise IdP | Heavier (JVM), admin UI has a learning curve, slower cold start |
| **Authentik** | Modern OSS IdP, Python-based, nicer UI, "flows" concept for custom login logic | Lighter than Keycloak, pleasant UI, good docs | Smaller community, less "this is what enterprises actually run" transfer value |
| **ORY (Hydra + Kratos + Oathkeeper)** | Unbundled OAuth2/OIDC server (Hydra) + identity management (Kratos) + API gateway (Oathkeeper) as separate services | Most architecturally "correct" separation of concerns — teaches you the pieces individually | Multiple services to stand up just to get a login screen; more moving parts than this learning goal needs |
| **Casdoor** | Lightweight OSS IdP, Go-based, simple to run | Very fast to stand up | Smaller ecosystem, less representative of what you'll meet in the wild |
| **FastAPI-Users** (library, not an IdP) | A FastAPI extension that adds user models + auth routes *inside your own app* | No separate service to run at all | This is "roll your own auth in FastAPI," not OAuth2/OIDC — doesn't teach the SSO concept this exercise is actually about |
| Roll-your-own (sessions, bcrypt, cookies) | Just build login/logout yourself | Simplest to understand line-by-line | Doesn't teach OAuth2/OIDC at all, and is exactly the kind of homegrown auth that's easy to get subtly wrong |
| Auth0 / Okta / Clerk (hosted) | SaaS IdPs | Fastest to integrate, excellent docs | **Not open source, not self-hosted** — ruled out by your own requirement, and also means the free tier disappears the moment this "lab" needs a second user |

**Leaning towards Keycloak** for the design doc, for one reason above the
others: this home lab already mirrors a real enterprise pattern (VMs,
Ansible, Always On AG) rather than toy setups, and Keycloak is what a real
enterprise is most likely to actually run. The other docs will build on that
choice — but re-read this table before committing if the Java/JVM weight
turns out to be annoying on the lab hardware; Authentik is a very reasonable
fallback with the same core learning value.

## Where this would plug into the existing app (conceptually)

```
┌─────────────────┐        ┌──────────────────────┐        ┌───────────────────┐
│   Browser         │        │   devops_VM1           │        │  devops_VM1 (same   │
│  (React SPA,       │        │   FastAPI :8000         │        │   VM, new container) │
│   Vite dev server   │        │   (Resource Server)      │        │   Keycloak :8080      │
│   or built bundle)  │        │                           │        │   (Authorization       │
│                    │        │   validates JWTs via      │        │    Server / IdP)       │
│                    │        │   Keycloak's JWKS          │        │                       │
└─────────┬──────────┘        └───────────┬───────────────┘        └───────────┬───────────┘
          │  1. redirect to Keycloak login                                       │
          ├──────────────────────────────────────────────────────────────────────►
          │  2. user logs in, Keycloak redirects back with an auth code           │
          ◄──────────────────────────────────────────────────────────────────────┤
          │  3. SPA exchanges code (+ PKCE verifier) for tokens, directly          │
          ├──────────────────────────────────────────────────────────────────────►
          ◄──────────────────────────────────────────────────────────────────────┤
          │  4. SPA calls FastAPI with `Authorization: Bearer <access_token>`     │
          ├───────────────────────────────►                                       │
          │                                 │ 5. fetches Keycloak's JWKS (cached) │
          │                                 ├──────────────────────────────────────►
          │                                 ◄──────────────────────────────────────┤
          │  6. FastAPI verifies signature/claims locally, allows/denies          │
          ◄───────────────────────────────┤                                       │
```

This is the **Authorization Code flow with PKCE** — the correct flow for a
browser SPA talking to your own API, as opposed to the deprecated Implicit
flow or the Resource Owner Password Credentials flow (both bad ideas for a
public client — the design doc covers why).

## Open questions to research before committing (design doc should answer these)

- Where does Keycloak actually run — a new container on `devops_VM1`
  alongside FastAPI, or its own VM? (Given the lab's existing "VM1 doubles
  as the controller" pattern, co-locating is probably fine for a learning
  setup.)
- Does FastAPI validate JWTs **locally** via JWKS (fast, no per-request call
  to Keycloak) or call Keycloak's **token introspection** endpoint on every
  request (simpler to reason about revocation, but a network round-trip per
  API call)? This is a real, common tradeoff worth deliberately learning.
- How do Keycloak roles map onto this app's existing endpoints? Naive idea:
  a `mssql-viewer` role for read-only endpoints (`ag-status`, `/history`,
  `/hosts`) and a `mssql-admin` role for anything that mutates the AG
  (`/failover`, `/sync-rebuild`, `/alwayson`, `/full-ag`, `/teardown`,
  `/rewind`) — worth confirming this mapping makes sense once the design
  doc lists every route.
- Where do tokens live in the browser — `localStorage` (simple, vulnerable
  to XSS token theft), an in-memory JS variable (safer, lost on page
  refresh unless paired with silent refresh), or a library that handles
  this for you (`react-oidc-context` does, using `sessionStorage` by
  default)?
- Does this lab ever need HTTPS for any of this to be meaningful, or is
  plain HTTP acceptable purely for local learning purposes? (Short answer
  you'll confirm in the design doc: OAuth2/OIDC *technically* works over
  HTTP for local dev, but bearer tokens over plaintext HTTP is a real
  anti-pattern worth calling out explicitly rather than silently doing it.)

## What "done" looks like for this whole exercise

By the end of the implementation doc, you should be able to:
1. Open the React app, get redirected to a real Keycloak login page, log in
   as a test user, and land back in the app with your username showing.
2. Call `GET /api/v1/deploy/ag-status` from the browser and see it succeed
   with a valid token, and see it **fail with 401** if you strip the
   `Authorization` header (test this in the browser devtools or via
   `curl`).
3. Log in as a `mssql-viewer` user and confirm `POST /api/v1/deploy/failover`
   is rejected with **403** (authenticated, but not authorized) — the
   distinction between 401 and 403 is itself a core learning point.
4. Explain, without looking anything up, what a JWKS endpoint is for and
   why the Resource Server doesn't need to call the Authorization Server on
   every request to validate a token.
