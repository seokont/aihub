# FORK_CHANGES.md — LibreChat fork ledger

Every modification to the LibreChat fork is recorded here, so that a future
upstream merge is mechanical rather than archaeological. Required by
CLAUDE.md §4.

## Integration model: submodule, and **zero** core modifications

**Decision (task 1.4): `ui/` is a git submodule pinned to the upstream tag `v0.8.7`.**

| | Submodule (chosen) | Subtree (rejected) |
| --- | --- | --- |
| Exact revision recorded | Yes — the gitlink SHA is in our history | Yes, but as vendored commits |
| Upstream history in our repo | No | Yes — tens of thousands of commits |
| Upgrade | `git -C ui fetch --tags && git -C ui checkout <tag>` + one gitlink commit | `git subtree pull`, a real merge each time |
| Third-party code in our history | None | All of it |
| Prerequisite | A parent repository | Works from a plain directory |

A subtree would have avoided the `git init` that 1.4 needed, but at the cost of carrying
LibreChat's entire history inside this repository — which is exactly the archaeology §4
wants to avoid, in reverse. The submodule keeps our history about *our* system.

**The fork HAS local modifications — entries 0002 and 0003 below — and they travel as a patch
series.** This section used to claim `git status` inside `ui/` was clean; it was true when the
integration model was decided and stopped being true when the OIDC work landed in 0002, so it is
corrected here rather than left as a header that contradicts its own ledger.

| | |
| --- | --- |
| Where the changes are | `infra/ui/patches/` — three patches, one per file, applied to the pinned checkout |
| Applied by | `make ui-patches`, a prerequisite of `make up` (`scripts/apply_ui_patches.py`) |
| Why patches, not a fork remote | `ui/`'s `origin` is upstream LibreChat, so the changes cannot be pushed there. Before the series existed they lived **only in the submodule's working tree**, and a fresh clone plus `git submodule update --init` silently produced an unpatched UI |
| Regenerating after editing the fork by hand | see `infra/ui/patches/README.md` |

The consequence to keep in mind when merging upstream: a `git checkout` **inside** `ui/` discards the
patches, and the resulting UI starts healthy and fails at login with an opaque `500` — it does not fail
to build. `tests/smoke/test_ui_patches.py` asserts the series is complete, that it touches only the
paths named in this ledger, and that it applies cleanly to the pinned commit.

Everything else MONI-specific still lives outside the submodule, in `infra/ui/`, and is mounted in at
runtime:

| What | Lives at | Mounted to |
| --- | --- | --- |
| LibreChat configuration | `infra/ui/librechat.yaml` | `/app/librechat.yaml` |
| Logo placeholder | `infra/ui/logo.svg` | `/app/client/public/assets/moni-logo.svg` |
| Container environment | `ui/.env` (generated) | via `env_file:` |

This is stronger than keeping the diff small: with no diff at all, an upstream merge is
`git checkout <new tag>` and a review of the config for renamed keys. The cost is that the
overlay files are *not* diffed against upstream by git, so a renamed upstream path surfaces
as a container that fails to start rather than as a merge conflict. That is why the compose
healthcheck asserts `/api/config` answers: a `librechat.yaml` that no longer parses, or a
mount that points at a path upstream moved, fails the healthcheck instead of quietly serving
a default-configured UI.

## Entry format

```
### <NNNN> — <short title>
- Date:            YYYY-MM-DD
- Upstream base:   LibreChat <tag or commit sha> (record the exact revision)
- Files touched:   ui/client/src/... , ui/api/...
- MONI change:     what was changed and why
- Upstream diff:   smallest possible? (yes/no — if no, explain)
- Merge strategy:  rebase onto <next tag> / re-apply patch / drop when upstream lands <feature>
- Rules touched:   CLAUDE.md §… (e.g. §3.1 UI talks only to the gateway)
```

## Merge strategy rules

- Keep the diff minimal and confined to branding, the approval-buttons component
  and separate MONI panel routes.
- Never modify core chat logic.
- Anything that can live outside the fork (a separate MONI panel bundle, an nginx
  route, a gateway endpoint) must live outside it.
- **Prefer configuration and mounts over edits.** A change that can be expressed as a
  mounted file or an environment variable does not belong in the fork at all.

## Entries

### 0001 — Add the LibreChat fork as a pinned submodule and wire the MONI overlay

- Date:            2026-09-25
- Upstream base:   LibreChat `v0.8.7`, commit `9e74cc0e57b395926122bd4062c1fcedc48ed465`
- Files touched:   **none inside `ui/`**. Added: `.gitmodules`, `infra/ui/librechat.yaml`,
  `infra/ui/ui.env.example`, `infra/ui/logo.svg`, services in
  `infra/docker-compose.dev.yml`, routes in `infra/nginx/dev.conf.template`,
  `scripts/gen_ui_secrets.py`.
- MONI change:     The UI is the front door. `librechat.yaml` declares a single custom
  endpoint ("MONI AI") pointing at `http://gateway:8080/v1`, and forwards each user's own
  Keycloak access token as a bearer header via `{{LIBRECHAT_OPENID_ACCESS_TOKEN}}` — with
  `OPENID_REUSE_TOKENS=true` making that placeholder resolvable. Local registration and
  email login are disabled so accounts can only come from Keycloak, which is what makes a
  user's Odoo credential mapping possible at all (§3.2). nginx serves `/` from the UI and
  keeps `/v1/` on the gateway; the Phase 0 dev index page is retired as the landing route.
- Upstream diff:   **None — this is the point.** Zero files under `ui/` were modified. The
  fork is a pristine checkout, so "merge upstream" is a tag change.
- Merge strategy:  `git -C ui fetch --tags && git -C ui checkout <new tag>`, then re-check
  the overlay for renamed config keys (`librechat.yaml` is schema-validated upstream: an
  unknown key fails startup). No patch to re-apply.
- Rules touched:   §3.1 (the UI's only upstream is the gateway; Mongo and Meilisearch
  publish no host port), §3.2 (per-user token forwarding, no shared key), §4 (fork
  discipline), §3.11 (all four published LibreChat default secrets are replaced by
  generated ones).

#### Why each non-obvious decision

- **`baseURL` is an admin-controlled URL, never `user_provided`.** LibreChat withholds
  configured header templates from user-supplied destinations. A user-provided URL would
  therefore drop the `Authorization` header silently — the request would reach the gateway
  unauthenticated and fail, or worse, a future default could make it succeed as the wrong
  identity.
- **A separate confidential Keycloak client (`moni-ui-web`).** `moni-ui` is public + PKCE
  and is what the integration tests drive with a password grant. Adding a secret to it would
  change what the tests exercise; creating a second client keeps each flow on the client
  built for it.
- **The audience mapper is attached to the UI client too.** LibreChat forwards the *access
  token*, and the gateway rejects a token whose `aud` is not `moni-gateway`. Without the
  mapper, login succeeds and every chat request 401s.
- **The default language is set by nginx, not by the UI.** LibreChat resolves its language
  as cookie → localStorage → `navigator.language`, with no `DEFAULT_LOCALE` setting. nginx
  sets `lang=uk` only when the browser has no `lang` cookie, so the UA default (§4) is applied
  once and a user's later choice in Settings is not overwritten. Meeting §4 this way is what
  keeps the diff at zero.
- **MongoDB authenticates.** LibreChat's own deploy compose runs `mongod --noauth`. The UI
  shares a network with the gateway and the MCP servers here, so an unauthenticated
  conversation store would be readable by any of them.

#### Known overlay risks (for the next upstream bump)

- `interface:` keys are schema-validated. During this task `interface.temporaryChat` was
  removed for exactly that reason — it is not in v0.8.7's schema, and an unknown key fails
  startup. Treat every `interface` key as needing verification against the target tag.
- `/app/client/public/assets/` is assumed to be the served asset root. If upstream moves it,
  the logo mount becomes a no-op rather than an error (the file simply is not requested).
- The UI image is built from the submodule (`Dockerfile.multi`, target `api-build`) rather
  than pulled from a registry, so the running UI is the pinned tag. This makes the build
  slower and is deliberate.

### 0002 — Permit plain-HTTP OIDC and redirect only the transport

- Date:            2026-09-25
- Upstream base:   LibreChat `v0.8.7`, commit `9e74cc0e57b395926122bd4062c1fcedc48ed465`
- Files touched:   `ui/api/strategies/openidStrategy.js`, `ui/api/strategies/openIdJwtStrategy.js`,
  `ui/api/strategies/oidcOrigin.js` (new shared module)
- MONI change:     Two dev-gated additions. (a) Pass `execute: [client.allowInsecureRequests]`
  to `client.discovery(...)`. (b) In `customFetch`, rewrite the ORIGIN of outgoing OIDC
  requests from `OPENID_ISSUER` to `OPENID_INTERNAL_ISSUER`, leaving responses untouched.
  Both are active only when `ALLOW_INSECURE_OIDC` is truthy **and** `MONI_ENV=dev`; both log
  a warning when they fire.
- Upstream diff:   **As small as it can be, and both halves are load-bearing.** Without (a),
  `openid-client` refuses the `http://` issuer with `OAUTH_HTTP_REQUEST_FORBIDDEN` and the
  strategy silently never registers — login is simply absent, with one error line. Without
  (b), discovery cannot reach Keycloak at all, because the container cannot resolve the
  public issuer's address.
- Merge strategy:  Re-apply by hand; the region is ~40 lines in one file. **Drop entirely**
  once Keycloak is served over TLS on a hostname that both the host and the containers
  resolve — at that point neither half is needed. `docs/runbooks/ui.md` records the removal
  steps.
- Rules touched:   §3.2 (this changes only *how the browser authenticates*; the identity the
  gateway receives is still the user's own access token), §3.11 (the relaxation is keyed on
  environment variables that must not be set in production), §4 (single, logged fork touch).

#### The identity/transport split — and the inversion that got it right

The first version of this entry did the opposite and was wrong. It pointed `OPENID_ISSUER`
at the compose service name and then **rewrote the `issuer` field of the discovery document**
so the two agreed. That produces a self-consistent discovery response whose issuer is a name
**no token will ever carry**: Keycloak stamps `iss` from its own `KC_HOSTNAME`
(`http://127.0.0.1:8081/realms/moni`), so the authorization response would be rejected by
openid-client's issuer check at the callback. The discovery step passing was a false signal.

The correct split, now implemented:

| Concern | Value | Why |
| --- | --- | --- |
| **Identity** — `OPENID_ISSUER`, what `iss` must equal, what the browser is sent to | `http://127.0.0.1:8081/realms/moni` | It is the address Keycloak advertises (`KC_HOSTNAME`) and therefore the only value that can appear in a token or in `authorization_endpoint`. |
| **Transport** — `OPENID_INTERNAL_ISSUER`, where fetches actually go | `http://keycloak:8080/realms/moni` | The UI container cannot reach the host loopback; it reaches Keycloak by service name. |

`customFetch` rewrites the request URL's origin and returns the response **unmodified**, so
the discovery document keeps advertising the public `issuer` and `authorization_endpoint`.
The browser is redirected to the public address (which it can reach, through the published
port) and the container fetches from the service name. Nothing about the identity is faked.

The rewrite is applied only to the origin of the expected public issuer, so a request aimed
anywhere else is left alone.

#### The second fetch path: `jwks-rsa` (this was a real bug, found later)

The first version of this entry redirected only `customFetch` and claimed the JWKS was
covered. **It was not.** `jwks-rsa`, which `openIdJwtStrategy.js` uses to resolve signing
keys, fetches over its **own HTTP client** and never passes through `customFetch`. So the
strategy was configured with the *advertised* `jwksUri` (`http://127.0.0.1:8081/...`), which
inside the container is the container itself, and key resolution failed with
`ECONNREFUSED`.

The symptom was maximally misleading: the token was valid, the algorithm was correct, the
audience was correct, and the only visible error was the HS256 fallback's
`"invalid algorithm"` — because `openidJwt` had failed on *key resolution* and
`requireJwtAuth` moved on to the `jwt` strategy.

Proven with a probe run inside the container:

```
advertised jwksUri : http://127.0.0.1:8081/realms/moni/protocol/openid-connect/certs
redirected jwksUri : http://keycloak:8080/realms/moni/protocol/openid-connect/certs
fetch redirected   : HTTP 200, keys=2
fetch advertised   : FAILED ECONNREFUSED
```

Both strategies now take the redirect from `api/strategies/oidcOrigin.js`, so the two fetch
paths cannot drift apart again.

#### Diagnostics added at the same time

`openIdJwtLogin` now logs its configuration once at startup and its own failure reason on
every rejection (key resolution, issuer mismatch, thrown error). Before this, a failure in
this strategy was invisible: `requireJwtAuth` fell through to the `jwt` strategy, and the
caller only ever saw the fallback's message. Startup line to look for:

```
[openIdJwtLogin] strategy configured: audience=["moni-ui-web","moni-gateway"] algorithms=["RS256"]
  jwksUri=http://keycloak:8080/... (redirected from http://127.0.0.1:8081/...)
```

That single line answers the three questions that previously took a rebuild to narrow down:
which audience is expected, which algorithms are accepted, and which JWKS host is used.

#### Why the escaping is acceptable here

`allowInsecureRequests` is openid-client's **own documented API**, not a hack around it: the
package exports the function precisely so a deployment may opt out of the TLS requirement.
The security cost is real and worth stating plainly — over `http://` the discovery document,
the token request and the JWKS are all unauthenticated, so an attacker on the path could
substitute Keycloak. That is acceptable only for a loopback dev stack, which is why the guard
requires two independent variables and why the code warns every time it engages.

The URL redirect has no upstream equivalent, but it does not weaken authentication: it moves
a request to a different address for the same Keycloak instance on a private compose network.
The alternative (TLS on a name resolvable by both the host and the containers) is deployment
work, tracked as such.

### 0003 — State the accepted signing algorithm for Keycloak bearer tokens

- Date:            2026-09-25
- Upstream base:   LibreChat `v0.8.7`, commit `9e74cc0e57b395926122bd4062c1fcedc48ed465`
- Files touched:   `ui/api/strategies/openIdJwtStrategy.js`
- MONI change:     Pass `algorithms: getOpenIdJwtAlgorithms()` to the `JwtStrategy` options,
  defaulting to `['RS256']` and configurable with `OPENID_JWT_ALGORITHMS`. Also adds
  diagnostic logging: the resolved configuration at startup, and the strategy's own failure
  reason on rejection.
- Upstream diff:   Small and self-contained: one option plus a small helper.
- Merge strategy:  **Prefer dropping this when upstream fixes it.** It is a genuine upstream
  defect, not a MONI requirement, so the right long-term answer is an upstream PR rather
  than a permanent fork touch. Re-check on each tag bump whether `algorithms` is still absent.
- Rules touched:   §3.2 (per-user tokens must actually authenticate), and the same
  algorithm-pinning principle the gateway already applies
  (`moni_gateway.security.ALLOWED_ALGORITHMS`).

#### The defect

`passport-jwt` forwards `options.algorithms` straight to `jsonwebtoken.verify`, and
`jsonwebtoken` **defaults to `['HS256','HS384','HS512']`** when it is undefined. A Keycloak
access token is RS256, so it was rejected with `{"message":"invalid algorithm"}` — every
time, for every client that presents one.

Two things make the symptom worse than it looks:

- `requireJwtAuth` tries the strategies in the order `['openidJwt','jwt']`, and `openidJwt`
  is registered whenever `OPENID_REUSE_TOKENS=true`. So the failing strategy is consulted
  **first**, making this the hot path for the very configuration the token-forwarding design
  depends on.
- The message is misleading. Measured directly against a real token:

  | Check | Result |
  | --- | --- |
  | RS256 signature, no audience check | VERIFIED |
  | RS256 + `audience=moni-ui-web` (the client id) | FAILED — *Audience doesn't match* |
  | RS256 + `audience=moni-gateway` | VERIFIED |
  | no `algorithms` stated | FAILED — *required that you pass in a value for the algorithms argument* |

  So the `openidJwt` strategy was failing on **audience**, falling through to the `jwt`
  strategy, and *that* one reported "invalid algorithm". Naming the algorithm fixes the
  first failure; the second is addressed in configuration, not code (see below).

Naming the algorithm is also the security-correct behaviour: verifying against a JWKS while
leaving the accepted algorithm unstated is how algorithm-confusion attacks are enabled.

#### The companion configuration (no code): `OPENID_AUDIENCE=moni-gateway`

Not a fork change — `infra/ui/ui.env.example` and `ui/.env` only — but it belongs in the same
entry because the two failures look identical from the client.

The realm's `moni-gateway-audience` mapper puts `aud: moni-gateway` into the access token
because the **gateway** rejects any token whose `aud` is not `moni-gateway`
(`moni_gateway.security`). LibreChat's own check expects `OPENID_CLIENT_ID`
(`moni-ui-web`). One token cannot satisfy both, and the gateway's requirement is the one that
cannot move — it is the §3.1 single-entry rule, not a preference. LibreChat validates against
`OPENID_CLIENT_ID + OPENID_AUDIENCE`, so listing the gateway audience is what makes one token
acceptable to both services.



