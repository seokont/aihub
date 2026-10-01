# Runbook — Zoho Mail (`mcp-zoho`)

Operating the mail tools: enabling them, reading their logs, and explaining what you find in the
mailbox. Task 2.5; see [ADR 0012](../adr/0012-untrusted-content-producer-and-zoho-surface.md) for why
the pieces are shaped this way.

## Enabling it

`mcp-zoho` is behind a compose profile, because it needs a real mailbox and a Zoho OAuth client that a
checkout does not have. Adding required `ZOHO_*` values to the default path would break `make up` for
everyone (compose interpolates the whole file even for a service a profile excludes), and a gateway
pointed at an absent `mcp-zoho` would fail every agent run waiting for it.

```bash
# .env: ZOHO_DC, ZOHO_ACCOUNT_ID, ZOHO_FROM_ADDRESS, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET,
#       ZOHO_REFRESH_TOKEN, and MONI_MCP_ZOHO_URL=http://mcp-zoho:8092/mcp
docker compose --env-file .env -f infra/docker-compose.dev.yml --profile zoho up -d --build mcp-zoho
docker compose --env-file .env -f infra/docker-compose.dev.yml up -d --build gateway
```

The gateway image must be rebuilt too: `MONI_MCP_ZOHO_URL` is read by `Settings`, and a gateway built
before the task does not know the variable exists — it will skip zoho silently and the model simply
will not be offered the mail tools.

`ZOHO_DC` is **not defaulted** (`eu` or `com`): a mailbox lives in one partition, and a token minted in
the other is rejected in a way that looks like a credential problem.

## If the container will not start

| Log line | Meaning |
| --- | --- |
| `zoho_startup_refused` naming a missing variable, exit 2 | A `ZOHO_*` value is absent or empty. The container refuses rather than serving requests it cannot fulfil |
| `zoho_auth_failed` | The refresh token was rejected. Zoho's error code is in the line; the token is **never** logged (§3.11) |
| `zoho_unavailable` | Transport or 5xx that survived the retry budget. Check outbound HTTPS from the container |
| container exits 1 with `ModuleNotFoundError` | The image's `mcp` SDK resolved to 2.x, which renamed `FastMCP`. Every MCP package must pin `mcp>=1.28,<2`; a smoke test asserts this |

## `[moni-test]` drafts in the mailbox

**If you see drafts whose subject is `[moni-test]` followed by twelve hex characters, they came from
the integration suite.** They are not a stuck run, a leak, or a failed send.

```bash
uv run pytest -m zoho tests/integration       # needs ZOHO_*; skips cleanly without them
```

The `zoho`-marked suite reads the mailbox, creates **one** draft per run, and asserts it landed in
Drafts and *not* in Sent. It deliberately does not tidy up after itself, and that is a decision rather
than an omission:

* deleting a draft would be a write beyond the four registered tools, and §3.3 says no tool may bypass
  the registry — a test is not an exception;
* the phase scope for the mail tools says no folder management.

So the residue accumulates at one draft per run of that suite, and this section exists so nobody has to
work out where it came from. Clear them by hand when it bothers you.

**The suite never sends.** The send path is exercised only through the approval gate in the in-chat
acceptance run, because an automated test that mails somebody is exactly the kind of surprise worth
avoiding. If you ever find a `[moni-test]` subject in Sent, that is a genuine failure of the
`create_draft`-does-not-send property and worth reporting as one.

## What is provisional

`send_message`'s endpoint is the shape the Zoho drafts documentation implies rather than one this
project has exercised: no mailbox was available while it was written, and that is stated in
`client.py` rather than smoothed over. The failure modes are not symmetric — a wrong endpoint fails
loudly (a 4xx on a send), while a wrong `create_draft` payload would have failed quietly by producing a
draft the user did not write. **The `zoho`-marked suite is what settles both**, so run it once with
real credentials before trusting the mail path.

## Verifying the §3.5 rule by hand

Reading any message must make the *rest of the run* behave differently: every write and irreversible
action after it needs approval, including in a scenario auto-mode would otherwise let through. The
observable evidence is one approval card per action and, in the log of a poisoned run,
`untrusted_context_set` naming the tool that raised it, then `approval_requested`.

`tests/unit/agent/test_untrusted_context.py` is the permanent proof, including a poisoned fixture
email instructing the model to send immediately. If you are testing this by hand, the interesting case
is **not** a plain send — that always asks — it is a send in a whitelisted scenario *after* a body has
been read.
