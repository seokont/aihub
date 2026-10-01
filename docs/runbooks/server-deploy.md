# Runbook — deploying the stack on a server

For standing the stack up on the GPU server (or any fresh machine). Every step is verifiable; where a
step can fail silently, the failure mode is named.

**Honest scope.** This is the Phase 2 procedure: the stack comes up, authenticates, runs the agent and
records an audit trail. Phase 2 is **not yet accepted** — see
[`phase2-audit-progress.md`](phase2-audit-progress.md) for which acceptance criteria are verified,
which are blocked and on what. The gaps that matter for a real deployment are listed at the end; read
them before promising anyone a date.

## 0. Prerequisites on the host

| need | why |
| --- | --- |
| `git`, `docker` + compose v2, `uv` | the deploy itself |
| outbound access to `github.com` | the UI submodule is a clone of LibreChat |
| the real `.env` (never from the repository) | §3.11: secrets live only in the environment |
| network reach to Odoo, Zoho, the model endpoint | the tools and the router call them |

## 1. Clone with the submodule

```sh
git clone <this repository> moni-ai
cd moni-ai
git submodule update --init --recursive ui
```

Skipping the submodule leaves `ui/` empty; `make up` then fails at the UI build with a missing-context
error. That failure is at least loud.

## 2. Apply the UI fork patches — **required, and silent if skipped**

```sh
make ui-patches
```

`ui/` is pinned at an **upstream** LibreChat tag, so the MONI changes are not in that commit: they
travel as a patch series in `infra/ui/patches/` (see
[the series README](../../infra/ui/patches/README.md) and `docs/FORK_CHANGES.md`). The `ui` service
builds with `context: ../ui`, so **an unpatched submodule builds an unpatched UI.**

This is the step that fails quietly if it is missed. The container starts, answers on its port and
reports healthy; the first login then fails with an opaque `500` from the OIDC strategy, and nothing in
the logs points at the cause. If you are debugging exactly that symptom, check this step before
anything else — [`ui.md`](ui.md) has the diagnosis.

`make ui-patches` is idempotent (a second run reports every patch as already applied) and refuses to
continue if the submodule is not at the pinned commit or if a target file has been changed outside the
series. `make up` runs it as a prerequisite, so the ordering is handled — run it explicitly if you are
building the UI some other way.

## 3. Provide the environment

```sh
cp .env.example .env
# then set every value that is a secret, and every value that is host-specific
```

`scripts/check_environment.py` (`make check-env`) verifies the file is complete against the compose
files and contains no committed secret. It is a real check, not a formality — but note the two things
it does **not** yet cover, both scheduled in `docs/BACKLOG.md` §8:

- `.env.example` ships a **working dev-default Langfuse key pair** on purpose (Langfuse mints keys only
  when it creates the project, so a concrete pair must exist on first start). If this deployment is not
  `MONI_ENV=dev`, replace `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
  `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY` and `LANGFUSE_ENCRYPTION_KEY`
  with values of your own. Until the deploy check in §8 lands, nothing refuses these defaults for you.
- the template's values are not yet audited against an allowlist, so a value filled in from a working
  `.env` and never blanked would not be caught.

## 4. Bring the stack up

```sh
make up          # applies the UI patches, builds, migrates, waits for health
make ps
```

`up` waits for health (`--wait`), so a smoke test of the perimeter is meaningful immediately after.

## 5. Map the Odoo users

```sh
make remap-odoo-users
```

Required after **every** Keycloak realm import: `--import-realm` mints fresh UUIDs, so every stored
subject addresses a user who no longer exists and tool calls fail closed with `unknown_user`. It also
repairs `TRIGGER_USER_SUB` — the identity a background run acts as — which goes stale in exactly the
same way and whose failure is quieter (§3.2: a triggered run acting as nobody is nobody's alert).

## 6. Check the perimeter (§3.1)

```sh
docker compose --env-file .env -f infra/docker-compose.dev.yml ps --format '{{.Service}}|{{.Ports}}'
```

Every published port must be `127.0.0.1`-bound. `scripts/check_environment.py` asserts this from the
compose file; this command asserts it of the running stack. The worker must publish **nothing**.

## 7. Verify

```sh
make check-env          # environment + secrets + loopback only
make check-migrations   # the DB revision matches the checkout
make test-unit          # unit + smoke
make test-integration   # needs the stack up
```

Then the acceptance procedure for the phase you are deploying — for Phase 2 that is the S22714
checklist in `docs/phases/phase2_tasks.md`, and the status of each criterion is in
[`phase2-audit-progress.md`](phase2-audit-progress.md).

## Known gaps — read before a real deployment

| gap | effect |
| --- | --- |
| Phase 2 is not accepted | the S22714 end-to-end criterion has not been executed on a live stand |
| The Zoho grant is missing `ZohoMail.folders.READ` | the mail trigger is armed and polls, and every poll fails with `INVALID_OAUTHSCOPE`; the full listing works |
| The local model must be reachable from the container | the host's `127.0.0.1` forward is not visible inside the container; a level-A run cannot start without it |
| `CLOUD_*` must point at an EU provider with no-training terms | the dev stand's current provider is **dev-only and US-based**, and must not survive into a production `.env` |
| The UI submodule is patched, not forked | a `git checkout` inside `ui/` discards the patches; re-run `make ui-patches` |
