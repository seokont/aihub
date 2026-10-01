# ADR 0005 — The OpenAI-compatible bridge, RBAC and per-user rate limiting

- **Status:** accepted (Phase 1, task 1.3)
- **Date:** 2026-09-25
- **Deciders:** MONI AI platform

## Context

Task 1.3 is the Gateway surface the UI talks to: `POST /v1/chat/completions` (SSE),
`GET /v1/models`, role→tool authorization, a per-user run limit and one audit row per run.

§2 fixes the shape — "LibreChat fork ── single entry: OpenAI-compatible endpoint + user JWT"
— and §4 says the fork must stay minimal ("do not modify core chat logic"). Four decisions
followed from that and are recorded here.

## Decision 1 — the gateway drives the agent in-process, for now

§2 draws Agent Core as its own box. In Phase 1 it runs **inside the gateway process**:
`moni_gateway.agent_runtime.default_agent_factory` builds the MCP client, the checkpointer
and the `AgentRunner` per request.

Reasons: Phase 1 runs are short and synchronous (a read question, ≤ 12 steps); §2's own
timeline puts the task queue in Phase 2, where the agent moves onto arq workers; and a
second HTTP hop would add a failure mode without adding a capability yet.

Consequences, stated plainly:

- `moni-agent` and `moni-router` are real **gateway** dependencies now, and the gateway
  image must contain them. It did not at first, and the chat route answered `502` with
  `ModuleNotFoundError` — this is exactly the kind of thing the image manifest should make
  impossible, so `tests/smoke/test_layout.py` asserts the Dockerfile copies each package.
- The gateway's dependency set is larger than §3.1's "small attack surface" ideal. When
  Phase 2 moves the agent to workers this reverts, and `agent_runtime` is the single seam.

## Decision 2 — authorization lives in `rbac.py`, never in a prompt

A model told to "only use allowed tools" is not an access control: a prompt is advisory and
a prompt-injected message can argue with it. So:

- the allow-list is computed from the **verified** JWT's realm roles, in code;
- the agent strips withheld tools from the schema it hands the model, so a withheld tool is
  *absent* rather than refused — the model cannot reason about what it cannot see;
- an unknown role grants the empty set, and an unclassified tool is treated as
  `irreversible` (§3.12 fail closed).

The role→tool map is the *tool-level* split the task specifies: manager → sales/partners/
tasks, warehouse → stock/deliveries, production → manufacturing, director/admin → all read.

**One deliberate asymmetry.** `get_sale_order` already returns the linked deliveries and
manufacturing orders, so a manager can answer the canonical "чому S22714 затримується?"
question without holding `get_deliveries` / `get_manufacturing_orders`. The broad list tools
stay with the roles that own those processes; the follow-the-order tool is the
cross-functional view a manager legitimately needs. A unit test pins that so a future
tightening of the manager role cannot silently break the canonical scenario.

`accountant` and `developer` are listed as **granting nothing**, and that is a decision, not
an omission: no tool in the Phase 1 set is a finance tool, and inventing one would mean
inventing a business rule (§8). `developer` must never hold production read tools.

## Decision 3 — rate limiting is per subject, and fails **open** on infrastructure failure

30 runs/hour per **Keycloak subject**, in Redis, as a fixed window (`INCR` + `EXPIRE`).

- **Per subject, not per IP.** The gateway sits behind nginx, so every request arrives from
  the proxy; an IP-keyed limit would make one office share a budget and punish a user for a
  colleague's usage. The subject is already verified when the limit is consulted.
- **Fixed window, not a sliding one.** Two atomic commands, no read-modify-write race, and a
  worst case of 2× the limit across a boundary — irrelevant for a cost-control limit.
- **Fail open when Redis is unreachable**, and log it. This is the *opposite* of §3.12, and
  worth being explicit about: §3.12's fail-closed rule governs authorization and data
  classification, where the safe answer is "no". A rate limit is a spend control, and
  refusing every run because a cache hiccuped converts a degradation into an outage. A user
  who genuinely exceeded the limit is still refused, and a Redis that answers is believed.
- Unset `MONI_REDIS_URL` → `NullRateLimiter`, which allows everything and marks the decision
  `degraded` so the route logs it.

The counter increments even when a request is refused, so a caller cannot keep their window
alive by hammering; the window advances on the clock, never on the caller's behaviour.

## Decision 4 — what the streaming path guarantees

SSE uses the **standard** OpenAI chunk framing (role chunk → content → `finish_reason` →
`data: [DONE]`) because LibreChat parses exactly that; a bespoke progress envelope would push
protocol work into the UI fork that §4 asks us to keep minimal.

The answer arrives as one chunk: the agent's text is produced by `respond` at the end of the
loop, so there is nothing incremental to forward. Node and tool progress is available as SSE
comment frames, which unaware clients ignore.

Two consequences worth stating:

- **Once the stream has begun, a failure cannot become an HTTP status.** It is sent as an
  `{"error": ...}` frame followed by a terminal chunk, which is the only honest option left.
- **The audit row is shielded from client disconnect** (`asyncio.shield`). A browser that
  navigates away cancels the response task; without shielding, that cancellation would
  propagate into the audit write and the run would vanish from the trail — the exact silent
  gap §3.8 exists to prevent.

## Alternatives considered

1. **A separate `agent` HTTP service now.** Rejected for Phase 1: it adds a hop and a failure
   mode before the task queue makes it necessary.
2. **Trusting a model-reported identity or tool list.** Rejected outright (§3.2, §3.3).
3. **Failing closed on a Redis outage.** Rejected: it turns a cache problem into a total
   outage for a limit that only bounds spend. Logged loudly instead.
4. **A gateway-local `ToolSpec` import instead of a Protocol.** Rejected: `rbac.py` needs
   only `.name`, and importing the router there crash-looped the container.
