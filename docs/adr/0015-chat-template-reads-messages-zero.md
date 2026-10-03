# ADR 0015 — The served chat template reads `messages[0]`, and nothing else

**Status:** accepted (2026-10-02). **Supersedes nothing; corrects an assumption that was never
written down** — that a LangChain `SystemMessage` anywhere in the list is an instruction the model
receives.

## Context

A chat run answered «Не вдалося отримати дані з Odoo для цього запиту…» although `get_my_tasks` had
returned `ok: true` with 43 tasks. Two separate causes were found, and the second is the larger one.

**The empty completion.** Against the server stand's model — vLLM 0.28.0, `--reasoning-parser
openai_gptoss`, `--tool-call-parser openai` — 10 of 10 attempts on the real `respond` shape returned
`finish_reason=stop`, `content_len=0`, and a populated analysis channel (141 chars, 65–66 completion
tokens). A plain chat call with no agent prompts returns content normally, so the model is capable of
the final channel; `finish_reason=stop` rules out truncation, and the per-node budgets already cover the
earlier `length` variant, so raising them again could not help.

**The template, read rather than inferred.** The served gpt-oss Harmony template
(`chat_template.txt`, the HF snapshot on the server) does two things this system did not expect:

1. **Only `messages[0]` is an instruction.** It renders `messages[0]` as the `developer` message and
   iterates the rest, handling `assistant`, `tool` and `user`. A `system` role in that iteration
   matches **no branch and is silently dropped**.
2. **An `assistant` message becomes the assistant's own `final` channel output.** With a generation
   prompt requested, it renders `<|start|>assistant<|channel|>final<|message|>{content}<|end|>` and then
   another `<|start|>assistant` to begin generating.

The graph appended its per-node instruction — and the plan — as **further system messages**
(`plan.md`, `act.md`, `verify.md`, `respond.md`), and passed tool results to `verify` and `respond` as a
synthetic **assistant** message. So on the local stand:

* the per-node instructions were **never seen at all**, including `respond.md`'s hard requirements
  about reporting identifiers verbatim and never presenting an alternative for a failed tool;
* `act` ran without its plan and re-derived the tool from the question alone;
* `verify` and `respond` were shown the tool results as **the assistant's completed final answer** — the
  model believed it had already answered, emitted analysis only, and stopped.

The measured fix, one variable at a time against the server model:

| shape | result |
| --- | --- |
| baseline (`[system, system, user, assistant(evidence)]`) | `REASONING_ONLY` 5/5 |
| evidence dropped entirely | `REASONING_ONLY` 5/5 |
| two system messages merged into one | `REASONING_ONLY` 5/5 |
| system prompt removed | `REASONING_ONLY` 5/5, one attempt `finish_reason=length` at 9918 chars |
| explicit "reply with the final answer only" appended | `REASONING_ONLY` 5/5 |
| **evidence in the user turn** | **`CONTENT_OK` 5/5** |
| plain chat (no agent prompts) | `CONTENT_OK` 5/5 |

## Decision

### 1. Exactly one system message, and it is `messages[0]`

`_conversation` now composes **one** `SystemMessage` from `system.md` plus, optionally, the plan and the
node's own instruction, and every node builds its prompt through it. A node cannot append an
instruction; there is nowhere for it to go.

### 2. `include_plan` is opt-in, and only `act` opts in

The plan is model-generated prose that a poisoned user message can steer, and the function this
replaces (`_evidence`) carried a deliberate rule that the plan is **not** evidence — treating it as such
launders a fabrication into the answer. Making the plan opt-in means a new node inherits the safe
behaviour rather than the convenient one. The exposure this creates is not new: on the **cloud** path a
provider concatenated the system messages, so the plan was already reaching the model there.

### 3. Tool results travel as `tool` turns, never as an assistant message

`verify` and `respond` send the real conversation, whose tool results are already paired with the
assistant turn that requested them. The template renders `role: tool` into
`functions.<name> to=assistant<|channel|>commentary`, which is the channel designed for tool output. The
synthetic evidence blob is gone; `_evidence` is deleted rather than left as an unused helper, and its
reasoning lives on as the `include_plan` docstring and here.

### 4. The shape is pinned by tests, per node

`tests/unit/agent/test_message_shape.py` asserts, for `plan`, `act`, `verify` and `respond`: exactly one
system message and it is `messages[0]` carrying that node's instruction; every tool result is a `tool`
turn answering a preceding assistant turn; and the prompt does not end on an assistant turn. It also
asserts the same messages are valid on the **cloud** path and that the instruction appears **exactly
once** — a merge that also re-appended the original would look like success while telling the model
everything twice.

## Consequences

* **The two destinations now agree.** Behaviour had been silently different between local vLLM (where
  instructions and plan were dropped) and the cloud (where providers concatenate system messages). A
  finding about "the model" was partly a finding about one server's template.
* **Prompts that were previously inert are now live.** `respond.md`'s requirements, `act.md`'s tool
  discipline and `verify.md`'s verdict format take effect for the first time on the local path. Their
  wording has never been exercised against a real model, so the first runs on the server stand are a
  first reading of them, not a regression check.
* **A future node must not append a system message.** The test is the guard; the failure is invisible
  otherwise, which is exactly why this went unnoticed for as long as it did.
* **What would falsify this:** a probe showing `CONTENT_OK` on the baseline shape (evidence as an
  assistant turn) on the pinned server model. The sweep above is the evidence, and it is re-runnable
  with `scripts/probe_model_shape.py --variant`.
* **Not verified from this machine.** The 10/10 acceptance on `plan`, `verify` and `respond` plus one
  human-read output has to be run on the server stand, where the model is; the tunnel to it is not
  reachable from the development host.
