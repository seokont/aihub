# ADR 0012 — The untrusted-content producer, and the zoho-mcp surface

- **Status:** accepted (task 2.5)
- **Date:** 2026-09-28
- **Deciders:** MONI AI platform

## Context

§3.5 has been in CLAUDE.md since the beginning: *"If the context contains external content (email
body, web page, WhatsApp message), any `write`/`irreversible` action in the same run REQUIRES approval
regardless of whitelist."*

Task 2.1 built the entire downstream half of that rule and proved it. `moni_gateway.policy.engine`
carries `ContextFlags.untrusted`, checks it **before** the whitelist — the order is itself a recorded
decision, in ADR 0007 — and `tests/unit/gateway/test_policy_matrix.py` drives the full cross-product of
five action classes × whitelisted × untrusted. The gateway's `policy_client` passes the flag through.
`AgentRunner` even read it: `graph.py` called `policy.decide(..., untrusted=bool(state.get(
"untrusted_context")))`.

Then task 2.5 arrived to produce the first external content in the system, and the audit that preceded
it found that **nothing had ever set that flag**. `AgentState` did not declare `untrusted_context`, no
code wrote it, and the key was read from a `TypedDict` that had no such key — `state.get(...)` on an
undeclared key returns `None`, so the expression was permanently `False` and no test noticed because
every test that cared passed the flag in by hand.

A rule with no producer is not a rule. It is a comment with tests around it.

## Decision

**1. The producer is the tool's own payload.** A tool that has just returned content written by
somebody outside the company sets `untrusted: true` at the top level of what it returns. The agent's
`observe` node — the only place a real tool payload arrives, since `act` merely dispatches — reads it
via `mcp_tools.marks_untrusted` and raises `untrusted_context: True` in the state it returns.

The comparison is `is True`, not truthiness: these payloads cross a JSON boundary and `"false"` is a
truthy string. A marker that raises the flag when it says false would teach everyone to ignore it.

**2. The producer lives in the payload, not in a table in the agent.** The alternative — a list of
"tools whose output is external" in `moni_agent` — was rejected for the same reason the agent owns no
copy of the action-class registry (§3.3): a second list drifts from the first, and *this* drift is
silent. A tool whose output stopped being recognised as external would simply stop raising the flag,
which is a §3.5 bypass with no error anywhere. The component that knows what it just returned is the
component that should say so.

**3. The flag is sticky for the run and can only be raised.** `observe` returns the key only when it
is true, and LangGraph merges the keys a node returns, so a run that has read an outsider's text
cannot clear the flag by reading something innocuous afterwards. That is the property the acceptance
test for stickiness exists to hold.

**4. `AgentState` declares it.** The key is a real field with a documented meaning rather than a
`get()` against a dictionary that never had it — which is what made the defect invisible to both mypy
and the test suite.

## Consequences

- **The rule now fires in the direction that matters.** A poisoned fixture email — an outsider's text
  instructing the model to send immediately — cannot reach a send even with auto-mode granted, and
  `tests/unit/agent/test_untrusted_context.py` asserts exactly that, permanently.
- **The discriminating case is the whitelisted one, and that is a testing decision worth recording.**
  `send_message` is `irreversible`, so it requires approval on its own and a naive poisoned-body test
  would show a pause either way — agreeing with itself. §3.5's *unique* contribution is that it makes
  the whitelist irrelevant, so the regression test grants auto-mode. Its twin omits the marker and
  shows the send running: same script, one difference, opposite outcomes.
- **`list_messages` is marked untrusted too.** A snippet is the opening characters of the same
  outsider's text; classifying the list as internal while the fetch is external would put a client's
  first line on the cloud wire depending on which tool the model picked.
- **The prompt framing is the weaker half and is labelled as such.** Untrusted results are wrapped in
  `<<<UNTRUSTED_EXTERNAL_CONTENT>>>` … `<<<END_…>>>` with a data-not-instructions preamble, and the
  system prompt names those markers. It is not a control: a persuasive email is exactly the input that
  talks a model out of a preamble. It exists so the honest reading is available to the model, and a
  test asserts the prompt and the code name the same markers — a prompt describing markers nothing
  emits is worse than no framing, because it tells the model to trust a boundary that is not there.
- **Two shapes in the Zoho client are provisional and say so.** `send_message`'s endpoint is the shape
  the drafts documentation implies rather than one this project has exercised, because no mailbox was
  available while it was written. The failure modes are not symmetric: a wrong endpoint fails loudly,
  while a wrong `create_draft` payload would fail quietly by producing a draft the user did not write.
  Both are stated in the code, and the `zoho`-marked live suite is what settles them.
- **A fourth MCP server made two "expected set" assertions stale, and that is the pressure working.**
  The action-class registry requires every advertised tool to be classified, and the classifier's
  `TOOL_SOURCES` requires every registered tool to have a source. Both failed on the first run after
  the tools were registered, which is how a new tool is forced to be a deliberate decision about what
  it *is* (§3.4) as well as what it may *do* (§3.3).
- **`send_message` is the project's first genuinely `irreversible` tool.** ADR 0009 argued that calling
  a recoverable action `irreversible` "to be safe" makes a class that lies, and a class that lies is
  one nobody can reason about when the real one arrives. It arrived: a delivered email cannot be
  recalled, and `create_draft` beside it stays `write` because a draft can be deleted.
