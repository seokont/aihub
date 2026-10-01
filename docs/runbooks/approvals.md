# Runbook — approvals

Task 2.1 built the storage, the state machine and the API; task 2.2 wired them to the agent. An
approval is the record of a human decision that a risky action may proceed (§3.3). Before a `write` or
`irreversible` tool runs, the policy engine returns `require_approval`, the agent **pauses**, a row
appears here in state `pending`, and a human decides. There are two ways to decide: the JWT API, and the
signed link the chat card carries (task 2.2b).

```
policy engine ──require_approval──► agent pauses (checkpointed) ──► approvals row (pending)
                                                                        │
        GET  /v1/approvals?status=pending   (JWT)                       │  the owner sees it
        GET  /approvals/{id}?t=<token>      (the link, task 2.2b)        │  the frozen call
        POST /v1/approvals/{id}/decision    (JWT)                       │  approve | deny, once
        POST /approvals/{id}  t=… decision=…(the link)                  ▼
                          approved | denied | expired   (terminal)
                          + one audit_log row: approval.decided  (channel: link when by link)
```

## The write path, and what an approved write actually does (task 2.3)

Two tools write, both `action_class="write"`, both **dev-gated until task 2.5**:

| tool | what it writes | Odoo mutation |
| --- | --- | --- |
| `create_project_task` | one `project.task`, assigned to a resolved user | `project.task.create` |
| `post_order_message` | one internal chatter note on a sale order | `sale.order.message_post` |

The chain for an approved write, in order:

```
agent computes the idempotency key   sha256(run_id + step_id + tool + canonical args)
        │                            (hash of the arguments AS RECEIVED — never of resolved values)
        ▼
human approves ──► the frozen call is re-dispatched verbatim ──► MCP server injects the key
        │
        ▼
odoo_idempotency: key already done?  ──yes──►  return the recorded id, DO NOT call Odoo
        │ no                                         (and do not even re-resolve the assignee)
        ▼
   claim the key  ──already in_flight?──►  typed refusal, DO NOT call Odoo
        │ claimed (new, or a failed_precommit row re-claimed)
        ▼
   Odoo create (with the calling user's own API key) ──► mark the row done with the returned id
        │
        └─ Odoo refused before writing ──► mark the row failed_precommit: retryable on the same key
```

Nothing about this is reachable outside a dev stand: the tools are not advertised unless `MONI_ENV=dev`,
no role table grants them, and `write` still means `require_approval` in the policy engine.

**Reading the ledger** — the first thing to look at when a write "did not happen". Run it *inside the
`mcp-odoo` container*, which is where the write path runs and therefore where `MONI_ENV` and
`DATABASE_URL` are the ones that were actually used:

```bash
docker exec moni-ai-dev-postgres-1 psql -U moni -d moni -c \
  "SELECT key, odoo_model, odoo_id, state, created_at
     FROM odoo_idempotency ORDER BY created_at DESC LIMIT 20;"

# anything stuck? an in_flight row is a write whose outcome is unknown; a failed_precommit row is a
# write Odoo refused before writing, and needs no reconciliation
docker exec moni-ai-dev-postgres-1 psql -U moni -d moni -c \
  "SELECT key, odoo_model, state, created_at, now() - created_at AS age
     FROM odoo_idempotency WHERE state <> 'done' ORDER BY created_at;"
```

**`state = 'in_flight'` and no `odoo_id`.** The write was attempted and never concluded. The record may
or may not exist in Odoo, and **the ledger cannot tell** — that is why it refuses instead of guessing.
The correct response is to reconcile by hand: search Odoo for the record the tool would have created
(for `create_project_task`, the task title) and then either leave it in place or remove it deliberately.
**Do not delete the ledger row to "unblock" it**: a new attempt of the same run step with the row gone
is exactly the duplicate the row prevents. Run the request again instead — a new run has a new
`trace_id` and therefore a new key.

**`state = 'failed_precommit'`.** Odoo *answered* and refused the create — an `AccessError` from its
ACL, or a `ValidationError`/`UserError` from its ORM — so nothing was created and **nothing needs
reconciling**. The row is deliberate evidence that the attempt happened and was refused, and the next
attempt with the same key re-claims it and runs the create again. A retry is therefore the correct
operator action for a key in this state; fix the user's Odoo access first, or the retry will be refused
the same way. If it keeps happening, the refusal is a policy answer rather than a fault: see the
`AccessError` note below. A failure that is *not* in that family — a timeout, a connection reset, a 5xx,
a malformed answer — is not proof that nothing was written and still leaves the row `in_flight`, which
is the ambiguous case above.

**A replay costs nothing.** When the same step is executed twice with the same key, the second
execution returns the recorded id and makes **no Odoo request at all**, including no assignee lookup.
The tool's response says `"replayed": true`, and its note states that the assignee was deliberately not
re-resolved. If you see `"created": true` a second time for what you believe was one step, the two
executions had different keys — compare `run_id` and `step_id` (they are `trace_id` and the step in
`steps_taken`), which is what `moni_agent.idempotency.key_material` renders for exactly this diagnosis.

**An `AccessError` is a refusal, not a fault — and since the 2.3 amendment it is retryable.** If the
user's Odoo role forbids the write, the tool returns `{"error": {"code": "odoo_access_error", ...}}` —
the same shape a forbidden *read* produces — and it is attempted exactly once. Nothing retries it
automatically, at either layer: the agent's retry budget reacts only to a transport `ToolError`. What
changed is the ledger row: Odoo answered before writing, so the key is `failed_precommit` and a later
attempt of the same step with the same key is allowed to run rather than refused as ambiguous. Do not
read the refusal as a transient failure either way: the fix is the user's Odoo access, which is the
platform's job to grant and not the agent's to route around (§3.3).

**An ambiguous assignee creates nothing.** Several users matching the query is
`odoo_ambiguous_match` with the candidates listed (id, name and login). Re-run naming the login. The
candidate list is deliberately short — ids, names and logins, never whole `res.users` rows (§3.11).

### `failed_precommit` vs `in_flight`: what an operator does

Both states mean "this key did not reach `done`", and they need **opposite** responses. Read the
`state` column before doing anything:

| | `in_flight` | `failed_precommit` |
| --- | --- | --- |
| What it means | an attempt ran, or ended with an outcome we cannot know | Odoo **answered and refused** before writing |
| Is the record in Odoo? | **unknown** — it may exist | **no**, by construction |
| Operator action | reconcile by hand: search Odoo for the record the tool would have made (for `create_project_task`, the task title), then decide | fix the user's Odoo access and **retry the same key** |
| Retry the same key? | **never** — retrying is precisely what duplicates | yes: the row is re-claimed in place and the create runs again |
| Delete the row? | **no** — a new attempt with the row gone is the duplicate the row prevents; run the request again (a new run gets a new `trace_id` and therefore a new key) | no need — the state is already the retryable one, and the row is the evidence that a refusal happened |
| Automatically retried? | no | no (the refusal is a policy answer, not a transport failure; §3.3) |

Two things are worth saying plainly, because both were expensive to learn:

* **A `failed_precommit` row is a fact, not an absence.** It exists so the refusal is visible to an
  operator and countable by Phase 3's success statistics. "Retryable" is an explicit state rather
  than a missing row, so the difference between "never attempted", "attempted and refused" and
  "attempted and unresolved" survives a restart.
* **Only a *pre-commit* refusal is retryable.** `AccessError` and the `ValidationError`/`UserError`
  family are the members, and the classification is by error *type* — a timeout, a connection reset,
  an Odoo 5xx, a malformed answer or a rejected credential all leave the row `in_flight`, because
  none of them is evidence about what the database now holds. Guessing "nothing was created" wrong is
  how a duplicate happens, so the conservative answer is the status quo.

**What the ledger will not tell you, and where the live proof stops.** A failure *after* Odoo's
transaction has committed (a `mail.thread` post-hook raising on an otherwise created task) leaves a
record that exists and a row that stays `in_flight`. That is genuinely ambiguous, and the ledger
refuses the next attempt rather than guessing.

The DEV fixture that demonstrates the `failed_precommit` half **against real Odoo** is the
**restricted fixture**: Keycloak user `viewer` (realm role `manager`, sub in
`MONI_ODOO_RESTRICTED_SUB`), whose Odoo side is the user `viewer@moni.test` — a **Portal user**,
with no `Project / User` group, so `project.task.create` is refused by Odoo's own ACL. It is a
Portal user rather than an "Internal User only" because Odoo 19's To-do app grants every *internal*
user create on `project.task` and Odoo unions applicable ACL rows, so an internal user cannot
demonstrate the refusal; the provisioning script's probe is what established that and is kept for
exactly this reason. Provision it with:

```bash
uv run --group dev python scripts/provision_projectread_user.py --dry-run   # report, write nothing
uv run --group dev python scripts/provision_projectread_user.py             # create what is missing
uv run --group dev python scripts/remap_odoo_users.py                       # map it
```

The script is parameterised (`--fixture`, `--groups`, `--sub-var`, `--credential-var`), and those
defaults build exactly this fixture. It is idempotent, and it ends by **probing** the refusal (a real
`project.task.create` as that user) rather than assuming that a group name produces one — Odoo's ACL
tables differ between versions and between customised databases.

Its Odoo half needs an operator credential permitted to write `res.users`/`res.groups`; the mapped
`manager@moni.test` is **not** such an account on this DEV instance. Per the restricted-fixture
runbook's decision 3 that credential is supplied as an **in-shell environment variable for single
commands** and never written to `.env`, code, docs or `odoo_user_map` (the script checks the process
environment first and the older `ODOO_PROJECTREAD_PROVISION_*` placeholders second). With neither set
the script stops after the Keycloak half and says so rather than reporting a fixture it did not build.
The full sequence, including the live proof and the group grant/revoke that makes the retry
observable, is [`restricted-fixture-proof.md`](restricted-fixture-proof.md).

## Verifying the write path against a live stand

The unit suite drives the real client and a fake session, so it cannot show that **Odoo** and the
ledger agree about the record. That is what the integration suite is for, and it is skipped unless DEV
Odoo is configured:

```bash
uv run --group dev pytest -m odoo tests/integration/odoo -q
```

It proves: one key produces exactly one task **counted in Odoo**; the `done` row's `odoo_id` names that
task; the full approval loop (pause → approve → resume) creates exactly one task and creates nothing
before the approval; and a denied approval writes nothing.

**Three findings, verified against the live DEV stand** (§3.7's acceptance run depends on all three):

1. `project.task.project_id` is **not required** (`required=False` on the live `fields_get`;
   `addons/project/models/project_task.py` declares it `compute=…, store=True, readonly=False` with no
   `required=True`), so a task with no project is a valid *private* task and no default project was
   invented. The integration suite asserts the created task's `project_id` is `False`.
2. The assignee field is **`user_ids`**, a `Many2many` to `res.users` on the live model, and there is
   **no `user_id` field at all** on Odoo 19 — so the tool sends a many2many command list. The
   integration suite asserts the assignee was stored.
3. `message_post` attributes the author as `env.user.partner_id` — the identity whose API key the
   client carries — so omitting `author_id` *is* the guarantee that the note appears as the calling
   user. Verified live: the message's `author_id` equalled the caller's own `partner_id`, and the
   subtype was `Note` with `internal = True`, so no follower was emailed. **Check it in the chatter**
   after a real write: post a note with `post_order_message` and confirm the author shown is the mapped
   user (the tool's response names the expected login as `author`). A live read-back of
   `mail.message.author_id` is deliberately not part of the tool — adding `mail.message` to the read
   allowlist would widen what the agent can read for a check an operator does once.

**One more thing the live check found, worth knowing before you debug a chatter write.**
`message_post` returns a **one-element list** on Odoo 19 (`[2141385]`), not a bare id. The client
accepts both shapes now, but the original code expected an `int` and every unit test passed on a
scripted one — so if a chatter write ever reports `odoo_protocol_error`, read the raw answer before
assuming the tool is broken.

## Seeding the S22714 fixture

```bash
uv run --group dev python scripts/seed_s22714.py --dry-run   # report, write nothing
uv run --group dev python scripts/seed_s22714.py             # create what is missing
```

It creates sale order **S22714** for an *existing* partner, ensures the order has a delivery that is
not done, creates an MO when MRP is installed (this is the one thing that writes to MRP — the tools
never do), and ensures the Odoo user «Максим» exists so `create_project_task`'s assignee resolution has
a real target. It is idempotent: every step looks its subject up first and reports `exists` rather than
creating a second one, so it is safe to run before every acceptance attempt. It refuses unless
`MONI_ENV=dev`, refuses a database whose name looks like production, and uses one named operator account
(`ODOO_TEST_MANAGER_LOGIN`) rather than a shared admin.

## Reading the current state

```bash
# through the API, as the user who owns them (own approvals only — another user's id is a 404)
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1/v1/approvals?status=pending'

# straight from the database
docker exec moni-ai-dev-postgres-1 psql -U moni -d moni -c \
  "SELECT id, user_sub, tool, action_class, status, created_at, expires_at, decided_by,
          (consumed_at IS NOT NULL) AS link_used
     FROM approvals ORDER BY created_at DESC LIMIT 20;"

# what happened to one of them
docker exec moni-ai-dev-postgres-1 psql -U moni -d moni -c \
  "SELECT ts, user_id, action, result, args_redacted->>'channel' AS channel, approval_id
     FROM audit_log WHERE action = 'approval.decided' ORDER BY ts DESC LIMIT 10;"
```

## The signed approval link (task 2.2b)

The card a paused run produces carries a link: `GET /approvals/{id}?t=<token>`. Opening it shows the
**frozen** call — tool, action class, arguments, deadline — and two buttons. The page is rendered by the
gateway (nginx routes `/approvals/` to it) and it is *not* part of the LibreChat fork: the human is
looking at a chat, and the gateway cannot post into that session, so the approval travels as a URL.

The token is HMAC-SHA256 over `v1.<payload>.<signature>`, keyed with `MONI_APPROVAL_LINK_KEY`, and the
payload names one approval, one key id (`jti`) and the approval's own expiry. It carries no subject,
tool or arguments (§3.11: a URL gets copied, pasted and logged).

**What makes a link stop working:**

| Situation | Result |
| --- | --- |
| The key is empty / unset | no link is minted; the page answers `503` (the pause is unaffected) |
| The signature does not match, or the URL names another approval | `403`, and the row is not even read |
| The token's expiry has passed | `403` |
| `approvals.link_jti` no longer matches the token's `jti` | `404` — the link was reissued or revoked |
| The approval was already decided | the page shows the outcome; a second `POST` is `409` |
| The link was already used to decide | same as above: `consumed_at` moves with the status |

**Revoking a leaked link** without rotating the shared key:

```sql
-- kill every token already issued for this approval
UPDATE approvals SET link_jti = NULL WHERE id = '<id>';
```

Mint a fresh one by re-sending the card (the next run, or `scripts/seed_approval.py`).

## Asking the conversation for its status

A **bare** status question in the same conversation is answered from our own state — the approvals row
pinned to the conversation's thread, plus the run's checkpoint for what actually executed — with **no
model call and no tool call**:

```
статус?   що там   ну що там   що з моїм запитом   чи готово   як справи
что там   как дела   status   any update   is it done
```

A question that names a record is **not** one of these. `статус замовлення S20013` (any digit
disqualifies it) goes to the agent and is answered from Odoo, which is the point: this path covers
"what happened to my request?", not "what is the state of order S20013?". Getting that boundary wrong
in the permissive direction would replace a real Odoo answer with a sentence about approvals.

The answers are composed in code (never generated) and the request writes exactly one audit row with
`result: status_from_state`. Live check against the running stack:

```bash
uv run --group dev python scripts/probe_status_answer.py
```

It seeds a pending approval on a known thread and asks `статус?`; then flips the approval to
`approved`, writes the checkpoint a resumed run would have left, and asks again. Each answer must name
the tool, must not mention Odoo, and must carry no tool calls. It prints PASS/FAIL per check and
removes both the row and the checkpoint afterwards.

## Creating one by hand (development only)

```bash
# prints the id, the URL to open in a browser, and the API path
uv run --group dev python scripts/seed_approval.py --sub <keycloak-sub> --tool echo_write
```

It goes through the same `SqlApprovalStore.create` path the agent uses, and mints a signed link the same
way, so running it also checks that creation works against the migrated schema and that the key the
gateway holds is the key you think it is. It is not installed as a console script and nothing in the
gateway calls it.

## The curl loop (no browser needed)

The whole decision path by hand, against the running stack. Set `$TOKEN` first
(`POST http://127.0.0.1:8081/realms/moni/protocol/openid-connect/token` with the `moni-ui` client,
`grant_type=password`, the manager test user and `MONI_TEST_USER_PASSWORD` from `.env`).

```bash
# 1. create a pending approval and print its link
uv run --group dev python scripts/seed_approval.py --sub "$SUB" --tool echo_write

# 2. the page renders the frozen call (through nginx)
curl -si "http://127.0.0.1/approvals/$ID?t=$LINK" | head -20

# 3. a tampered token is refused, and the row is untouched
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1/approvals/$ID?t=${LINK%?}X"   # 403

# 4. decide it, exactly as the page's button does (form-encoded, not JSON)
curl -s -X POST "http://127.0.0.1/approvals/$ID" \
  --data-urlencode "t=$LINK" --data-urlencode "decision=approve" \
  --data-urlencode "comment=by hand" | grep -o 'Підтверджено' | head -1

# 5. the row moved, the link was spent, and the trail says how it arrived
docker exec moni-ai-dev-postgres-1 psql -U moni -d moni -c \
  "SELECT status, decided_by, consumed_at IS NOT NULL AS link_used FROM approvals WHERE id = '$ID';"
docker exec moni-ai-dev-postgres-1 psql -U moni -d moni -c \
  "SELECT user_id, action, result, args_redacted->>'channel' FROM audit_log WHERE approval_id = '$ID';"

# 6. a second click is a conflict, and nothing is written twice
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1/approvals/$ID" \
  --data-urlencode "t=$LINK" --data-urlencode "decision=deny"                            # 409

# 7. the JWT route agrees: the approval is already decided
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1/v1/approvals/$ID/decision" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"decision":"deny"}'                                                              # 409
```

`tests/integration/gateway/test_approval_link.py` runs exactly this loop (through httpx rather than
curl) on every integration run, so the steps above are a debugging aid rather than the only coverage.

## Do not "clean up" the approval rows

The integration suite creates approval rows on every run and **does not delete them**, and that is
deliberate rather than an oversight:

- The audit trail is append-only (§3.8). A decision writes an `audit_log` row carrying the
  `approval_id`, so deleting the approval would orphan the record of who allowed what — leaving an
  audit row pointing at something that no longer exists, which is worse than an untidy table.
- The rows are evidence. A dev database with a hundred `approved` rows shows that the decision path
  ran a hundred times; an empty one shows nothing and cannot distinguish "never used" from "cleaned".
- `pending` rows from a test run are indistinguishable from a real one somebody is waiting on, so a
  blanket `DELETE ... WHERE status = 'pending'` is exactly the wrong instinct: it would silently
  discard the one row a human was actually being asked about.

A long-lived dev database accumulates them, which is visible in `GET /v1/approvals?status=pending`
(the integration fixtures add a few per run). **Leave them.** If the noise ever genuinely matters,
delete *by trace id* — `it-approval-…` for the integration suite — rather than by status, so the
audit trail stays joinable and no real pending row can be caught in the sweep.

## Expiry

`expires_at` defaults to 24 hours after creation. Expiry is applied lazily — on read, and again
before a decision — so the table never claims `pending` for something nobody can decide any more.
An expired approval:

- reports `expired` when listed or fetched;
- **cannot be decided**: the decision is a `409`, not a silent success;
- counts as *denied*, because the default is refusal (§3.12);
- is recorded as `decided_by = 'system:expiry'`, so a refusal by time is distinguishable from a
  refusal by a person.

To make one expire immediately when testing, move **both** timestamps — `expires_at` must stay after
`created_at`, which the table enforces:

```sql
UPDATE approvals SET created_at = now() - interval '2 days',
                     expires_at = now() - interval '1 day'
 WHERE id = '<id>';
```

## Rules an operator should know

- **Own approvals only.** Every route is scoped to the subject in the token; another user's approval
  answers `404`, never `403`, because a `403` would confirm the row exists and turn the endpoint into
  an oracle for someone else's pending actions. The **link** route is scoped differently and
  deliberately: the token names no subject, so the row is looked up by its stored `link_jti` — which is
  also what makes a link revocable on its own (ADR 0008, decision 6).
- **A decision is final.** There is no un-deciding and no re-opening; a second decision is a `409`
  and the first one stands. A mistake is corrected by doing the work again, with its own approval.
- **A link decision is attributed to the approval's owner**, and the audit row records
  `channel: link`. That is not the same as proof that the owner clicked: whoever holds the URL can
  decide. Treat a forwarded approval link like a password, and revoke it if it goes somewhere it should
  not have.
- **Nothing is decided automatically.** The `auto_mode_whitelist` table exists and is **empty** by
  design — promotion is Phase 3 and is a manual decision. If it is ever non-empty in an environment
  where nobody expected it to be, that is worth investigating rather than celebrating.
- **After changing `infra/nginx/dev.conf.template`, restart nginx** (`docker compose restart nginx`).
  The config is rendered from the template at container start, so an added `location` is invisible until
  then — the symptom is the SPA's `index.html` being served for a gateway route.
- **A new secret needs to be named in `infra/docker-compose.dev.yml` too**, not only in `.env` and
  `.env.example`: the compose file enumerates each service's environment, so `MONI_APPROVAL_LINK_KEY`
  set only in `.env` is invisible inside the gateway container. The symptom is the page answering `503`
  while the key is clearly present.

