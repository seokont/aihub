# zoho-mcp

Zoho Mail tools for the MONI AI agent: **read, draft, and send-with-approval** (Phase 2, task 2.5).

Four tools, one mailbox, and a rule that makes the whole thing worth having: §3.5's untrusted-content
rule. An email body was written by somebody outside the company, so reading one puts the *entire run*
into untrusted context, and every write after that requires a human — including in a scenario that
auto-mode would otherwise let through.

| Tool | Class | What it does |
| --- | --- | --- |
| `list_messages` | `read` | Message headers from a folder. Snippets are marked untrusted |
| `get_message` | `read` | One message's headers and text body, marked untrusted |
| `create_draft` | `write` | Saves a plain-text draft. **Never sends** |
| `send_message` | `irreversible` | Sends an existing draft. A delivered mail cannot be recalled |

## Why the reads are marked untrusted

`get_message` and `list_messages` put `untrusted: true` in their own payload. The agent's `observe`
node reads that marker and raises `untrusted_context` for the rest of the run — sticky, never reset —
and the policy engine forces approval for every `write` and `irreversible` action from then on,
**before** it consults the auto-mode whitelist.

So the poisoning defence is not "the model notices the injection". It is that being convinced changes
nothing without a human. The marker lives in the payload rather than in a table in the agent, because
the tool is the only component that knows what it just returned; a second list in the agent would
drift silently, and the drift would be a §3.5 bypass with no error anywhere.

`list_messages` is marked too, and that is deliberate rather than symmetric: a snippet **is** the
opening characters of the same outsider's text, so classifying the list as internal would send the
first line of a client's email to the cloud depending on which tool the model happened to pick.

## Running it

It is profile-gated, because it needs a real mailbox that a checkout does not have:

```bash
# .env: ZOHO_DC, ZOHO_ACCOUNT_ID, ZOHO_FROM_ADDRESS, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET,
#       ZOHO_REFRESH_TOKEN, and MONI_MCP_ZOHO_URL=http://mcp-zoho:8092/mcp
docker compose --env-file .env -f infra/docker-compose.dev.yml --profile zoho up -d
```

Without the profile — and with `MONI_MCP_ZOHO_URL` left empty, which is the default — the gateway
skips this server entirely and the rest of the stack is unaffected. That matters: pointing the gateway
at a `mcp-zoho` that is not running would make every agent run wait for it.

The server refuses to start when a `ZOHO_*` value is missing or empty (`zoho_startup_refused`, exit
2), rather than accepting requests and failing each one.

### Configuration

| Variable | Notes |
| --- | --- |
| `ZOHO_DC` | `eu` or `com`. **Not defaulted** — a mailbox lives in one partition and a token from the other is rejected, which looks like a credential problem |
| `ZOHO_ACCOUNT_ID` | The one mailbox this server acts for |
| `ZOHO_FROM_ADDRESS` | Required for a draft: the API mandates `fromAddress` and will only accept an address belonging to the authenticated account |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` / `ZOHO_REFRESH_TOKEN` | A Zoho OAuth client with Mail scopes. Secrets (§3.11) — never logged; even the auth-failure message carries only Zoho's error code |
| `MONI_MCP_ZOHO_HOST` / `MONI_MCP_ZOHO_PORT` | Transport binding. Inside the container the host **must** be `0.0.0.0`; `.env` holds `127.0.0.1` because that is where the published port is bound |

## API notes, including one thing that is provisional

Both hosts are **built from `ZOHO_DC`**, never hardcoded:
`https://accounts.zoho.{dc}` mints tokens and `https://mail.zoho.{dc}` serves mail. The second was
wrong first — `www.zohoapis.{dc}` is where Zoho's other APIs live — and every call against it would
have failed with a 404 or a misleading 401.

`folderId` is a **long**, not a name. `list_messages(folder="INBOX")` resolves the name through the
folders endpoint and caches the map; an unknown folder is **refused** with the known names in the
message rather than defaulting to INBOX, because silently listing the wrong folder is a wrong answer
that looks like a right one.

A reply needs `inReplyTo`, which is the RFC **Message-ID** (`<…@zoho.com>`) and *not* the `messageId`
this project passes around — the API does not accept one for the other, so the id is resolved through
the message header first and the reply is refused if there is no Message-ID to attach to.

> **`send_message`'s endpoint is unverified against the live API.** It is the shape the drafts
> documentation implies rather than one this project has exercised, because no mailbox was available
> while it was written. It is called out rather than smoothed over: a wrong endpoint fails loudly (a
> 4xx on a send), whereas a wrong `create_draft` *payload* would have failed quietly by producing a
> draft the user did not write. The `zoho`-marked integration suite is what settles it.

## Tests

* `tests/unit/zoho/` — the client over a recording transport (so the assertions are about the bytes
  that would leave the process, not about a stub's beliefs), the tool payload shapes, the caps, and
  the refusals.
* `tests/integration/zoho/` (marker `zoho`) — against a real TEST mailbox, skipped unless `ZOHO_*` is
  set. It **never sends**: it reads, creates one draft, and asserts it landed in Drafts and not in
  Sent. **It leaves that draft behind** — clearing it up would be a write beyond the four registered
  tools (§3.3), and the phase scope says no folder management. Drafts are prefixed `[moni-test]`.
* The rule itself is proven in `tests/unit/agent/test_untrusted_context.py`, including a poisoned
  fixture email that must not reach a send even with auto-mode granted.

Nothing here handles attachments, folder management, contact sync, auto-send, or HTML composition.
