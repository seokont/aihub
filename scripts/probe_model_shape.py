#!/usr/bin/env python3
"""Probe an OpenAI-compatible model for the empty-completion fault (Step 0 of the `graph.py:1302` fix).

**The question this answers.** A `respond` call in run `run-8802851d…` reported **243 completion
tokens** and empty `content`, so the agent fell back to a template that blamed Odoo even though the tool
call had succeeded. Two mechanisms produce that, and they need opposite fixes:

* **H1 — reasoning-only completion.** The model puts its text in an analysis/reasoning channel and never
  opens the final one. The answer would be *reading the other channel* or *changing the request*, and a
  blind retry would reproduce it forever.
* **H2 — truncation.** The generation is cut off before the final channel opens. Then the fix is the
  token budget (`NODE_MAX_TOKENS["respond"]` is 2048), and a retry may legitimately help.

This probe decides between them from `finish_reason`, the channels that actually came back, and how the
outcome changes with the budget. It does **not** guess at request parameters: `--extra-json` exists so
the operator can try whatever the served chat template accepts, instead of this script inventing flags.

The payload mirrors the agent's real `respond` call: the same prompt files, the same message order, no
tools. `--node` switches between the tool-free nodes.

Usage::

    uv run --group dev python scripts/probe_model_shape.py                       # 5 attempts
    uv run --group dev python scripts/probe_model_shape.py --repeat 20
    uv run --group dev python scripts/probe_model_shape.py --max-tokens 4096     # H2 test
    uv run --group dev python scripts/probe_model_shape.py --evidence-file ev.txt
    uv run --group dev python scripts/probe_model_shape.py --base-url http://corporate-llm:8000/v1

Exit codes: 0 when every attempt produced usable text, 1 when any attempt did not (so it can gate a
deploy check), 2 when the endpoint could not be reached at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Final

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = REPO_ROOT / "agent" / "src" / "moni_agent" / "prompts"

#: The budget the agent actually uses per node (`graph.py::NODE_MAX_TOKENS`), so a bare run reproduces
#: the production request rather than a friendlier one.
NODE_MAX_TOKENS = {"plan": 3072, "act": 1024, "verify": 2048, "respond": 2048}

#: A small, obviously-synthetic evidence block. It is labelled as a fixture inside the payload, because
#: a probe that looks like production data is a probe somebody will mistake for one.
SYNTHETIC_EVIDENCE = (
    "PROBE FIXTURE — not real data.\n"
    "get_my_tasks returned 43 tasks. First three: \n"
    "- 42: «Перевірити S22714» (state: In Progress, deadline 2026-10-05)\n"
    "- 43: «Підготувати звіт» (state: New, deadline none)\n"
    "- 44: «Замовити матеріали» (state: Done, deadline 2026-09-30)\n"
)

QUESTION = "Які мої задачі?"

#: One tool, declared so a variant can send a real assistant tool call followed by a real tool result.
#: The served gpt-oss template renders `role: tool` into the `functions.<name> to=assistant`
#: commentary channel, which is where tool output belongs — unlike an assistant turn, which it renders
#: as the assistant's own *final* channel message.
PROBE_TOOL: Final[dict[str, object]] = {
    "type": "function",
    "function": {
        "name": "get_my_tasks",
        "description": "List the calling user's Odoo tasks.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def prompt_text(name: str) -> str:
    path = PROMPTS / f"{name}.md"
    if not path.is_file():
        raise SystemExit(f"prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def build_messages(variant: str, node: str, evidence: str) -> list[dict[str, object]]:
    """The messages for one single-variable shape (Step 1).

    Every variant differs from ``respond`` in **exactly one** respect, because the question is which
    single change makes the model use its final channel instead of stopping after the analysis one.
    Two changes at once would answer nothing.

    ``respond`` and ``verify`` are the two nodes that end their prompt with an **assistant** turn, and
    ``plan`` is the one that sends **two system messages** — those are the structural suspects, and the
    variants exist to separate them.
    """
    system = prompt_text("system")
    instruction = prompt_text(node)

    if variant == "plain":
        # The control that is known to work: no agent prompts at all.
        return [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": QUESTION},
        ]

    if variant == f"{node}-no-system":
        return [
            {"role": "system", "content": instruction},
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": evidence},
        ]

    if variant == f"{node}-one-system":
        # One system message instead of two: some templates concatenate, some do not.
        return [
            {"role": "system", "content": f"{system}\n\n{instruction}"},
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": evidence},
        ]

    if variant == f"{node}-no-evidence":
        # Drop the trailing assistant turn — the shape suspect for respond/verify.
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": instruction},
            {"role": "user", "content": QUESTION},
        ]

    if variant == f"{node}-evidence-as-user":
        # The evidence as a *user* turn: same information, no trailing assistant turn.
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": instruction},
            {"role": "user", "content": f"{QUESTION}\n\n{evidence}"},
        ]

    if variant == f"{node}-merged-evidence-as-user":
        # BOTH fixes at once, and neither alone was enough to prove the combination:
        #   * the node instruction merged into messages[0], because the served template reads only
        #     `messages[0]` as an instruction and silently drops every later `system` role;
        #   * the evidence in the user turn rather than a trailing assistant turn, which is the one
        #     change that turned 0/5 into 5/5.
        return [
            {"role": "system", "content": f"{system}\n\n{instruction}"},
            {"role": "user", "content": f"{QUESTION}\n\n{evidence}"},
        ]

    if variant == f"{node}-merged-tool-turn":
        # The same instruction placement, but the evidence as a REAL tool result: an assistant turn
        # carrying the tool call, then `role: tool` answering it. The template renders that into the
        # commentary channel, which is where tool output belongs — and it keeps untrusted tool text
        # out of the *user* turn, which matters for the untrusted-content rule.
        return [
            {"role": "system", "content": f"{system}\n\n{instruction}"},
            {"role": "user", "content": QUESTION},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_probe",
                        "type": "function",
                        "function": {"name": "get_my_tasks", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_probe",
                "name": "get_my_tasks",
                "content": evidence,
            },
        ]

    if variant == f"{node}-answer-only":
        # Does an explicit instruction to skip the analysis channel change anything?
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": instruction},
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": evidence},
            {
                "role": "system",
                "content": "Reply with the final answer only. Do not use the analysis channel.",
            },
        ]

    # Baseline: byte-identical to what the agent's node builds.
    return [
        {"role": "system", "content": system},
        {"role": "system", "content": instruction},
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": evidence},
    ]


def variants_for(node: str) -> tuple[str, ...]:
    """The variant names offered for a node, so `--help`-level discovery is not guesswork."""
    return (
        node,
        f"{node}-no-evidence",
        f"{node}-evidence-as-user",
        f"{node}-one-system",
        f"{node}-no-system",
        f"{node}-answer-only",
        f"{node}-merged-evidence-as-user",
        f"{node}-merged-tool-turn",
        "plain",
    )


def build_payload(
    *,
    model: str,
    node: str,
    max_tokens: int,
    evidence: str,
    extra: dict[str, object],
    variant: str | None = None,
) -> dict[str, object]:
    """The request body for one node, in the chosen message shape."""
    payload: dict[str, object] = {
        "model": model,
        "messages": build_messages(variant or node, node, evidence),
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    if variant and variant.endswith("-merged-tool-turn"):
        payload["tools"] = [PROBE_TOOL]
    payload.update(extra)
    return payload


def classify(choice: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Name what came back, so H1 and H2 are distinguishable without reading prose."""
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    finish = choice.get("finish_reason")

    facts: dict[str, object] = {
        "finish_reason": finish,
        "message_keys": sorted(message),
        "content_len": len(content),
        "reasoning_len": len(reasoning),
        "tool_calls": len(message.get("tool_calls") or []),
    }

    if content.strip():
        return "CONTENT_OK", facts
    if reasoning.strip():
        # The text exists, in a channel the router does not read. This is H1.
        return "REASONING_ONLY", facts
    if finish == "length":
        # Cut off before anything usable was emitted. This is H2.
        return "LENGTH_TRUNCATED", facts
    return "NO_CHANNELS", facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="probe_model_shape", description=__doc__)
    parser.add_argument("--node", default="respond", choices=sorted(NODE_MAX_TOKENS))
    parser.add_argument(
        "--repeat", type=int, default=5, help="attempts, to measure how often it fires"
    )
    parser.add_argument(
        "--max-tokens", type=int, default=None, help="default: the node's own budget"
    )
    parser.add_argument("--base-url", default=None, help="default: VLLM_BASE_URL")
    parser.add_argument("--model", default=None, help="default: VLLM_MODEL")
    parser.add_argument("--api-key", default=None, help="default: VLLM_API_KEY")
    parser.add_argument(
        "--evidence-file",
        default=None,
        help="use this file as the evidence block instead of the synthetic fixture (replay real data)",
    )
    parser.add_argument(
        "--variant",
        default=None,
        help=(
            "ONE single-variable message shape (Step 1). Default: the node's real shape. Choices for "
            "--node respond are: " + ", ".join(variants_for("respond"))
        ),
    )
    parser.add_argument(
        "--dump-reasoning",
        action="store_true",
        help=(
            "print the reasoning channel in full. With the openai_gptoss parser that channel IS the "
            "analysis, so reading it is what decides whether it may ever be shown to a user (Step 3)"
        ),
    )
    parser.add_argument(
        "--dump-prompt",
        action="store_true",
        help="print the exact messages sent, so 'which prompt produced this' is never inference",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        default=None,
        help=(
            "set chat_template_kwargs.reasoning_effort, which the served template documents (default "
            "'medium'). Exists so this cannot be lost to shell quoting: pass the level, not JSON. "
            "--extra-json still wins if both are given."
        ),
    )
    parser.add_argument(
        "--extra-json",
        default=None,
        help=(
            "a JSON object merged into the request body, for trying served-template parameters "
            '(e.g. \'{"chat_template_kwargs": {"reasoning_effort": "low"}}\'). This script does not '
            "guess these for you."
        ),
    )
    args = parser.parse_args(argv)

    base_url = (args.base_url or os.environ.get("VLLM_BASE_URL") or "").rstrip("/")
    model = args.model or os.environ.get("VLLM_MODEL") or ""
    api_key = args.api_key or os.environ.get("VLLM_API_KEY") or ""
    if not base_url or not model:
        raise SystemExit(
            "VLLM_BASE_URL and VLLM_MODEL must be set (or passed as --base-url/--model)"
        )

    max_tokens = args.max_tokens or NODE_MAX_TOKENS[args.node]
    evidence = (
        Path(args.evidence_file).read_text(encoding="utf-8")
        if args.evidence_file
        else SYNTHETIC_EVIDENCE
    )
    extra: dict[str, object] = json.loads(args.extra_json) if args.extra_json else {}
    if args.reasoning_effort:
        # Merge rather than replace: `--extra-json` may already carry other template kwargs, and a
        # dict value out of the parsed JSON is not `object` to mypy, hence the narrow cast.
        existing = extra.get("chat_template_kwargs")
        template_kwargs: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
        template_kwargs.setdefault("reasoning_effort", args.reasoning_effort)
        extra["chat_template_kwargs"] = template_kwargs
    variant = args.variant or args.node
    if variant not in variants_for(args.node):
        raise SystemExit(
            f"unknown --variant {variant!r} for --node {args.node}. "
            f"Choices: {', '.join(variants_for(args.node))}"
        )
    payload = build_payload(
        model=model,
        node=args.node,
        max_tokens=max_tokens,
        evidence=evidence,
        extra=extra,
        variant=variant,
    )

    if args.dump_prompt:
        print("messages sent:")
        # `payload["messages"]` is typed `object` because the body is a plain dict for the wire; the
        # annotation here is what lets mypy follow it, rather than an ignore that would hide a real
        # change of shape.
        sent: list[dict[str, object]] = payload["messages"]  # type: ignore[assignment]
        for index, message in enumerate(sent):
            # `str` because `content` may legitimately be absent or empty (the assistant turn that
            # carries only a tool call), so this is a display of whatever is there, not an assertion.
            body = str(message.get("content") or "")
            print(f"  [{index}] {message.get('role')}: {len(body)} chars")
            print(f"      {body[:400]!r}")
        print()

    print(f"probe: {base_url}/chat/completions")
    print(f"  model={model}  node={args.node}  variant={variant}  max_tokens={max_tokens}")
    print(f"  attempts={args.repeat}")
    print(
        f"  evidence: {'file ' + args.evidence_file if args.evidence_file else 'synthetic fixture'}"
    )
    if extra:
        print(f"  extra body: {json.dumps(extra, ensure_ascii=False)}")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    counts: dict[str, int] = {}
    tokens: list[int] = []
    #: Attempts that ended on `finish_reason=length`. Reported separately from a blank answer because
    #: the fix differs: a truncated generation needs a bigger budget, a blank one needs a message shape
    #: the model will answer from.
    truncated: list[int] = []

    with httpx.Client(timeout=120.0) as client:
        for attempt in range(1, args.repeat + 1):
            try:
                response = client.post(
                    f"{base_url}/chat/completions", headers=headers, json=payload
                )
            except httpx.HTTPError as exc:
                print(f"\n  attempt {attempt}: endpoint unreachable: {type(exc).__name__}: {exc}")
                if attempt == 1:
                    print(
                        "\nFAILED: cannot reach the model. On the server, check that the container's"
                    )
                    print("  VLLM_BASE_URL resolves (the host's 127.0.0.1 is not the container's).")
                    return 2
                continue

            if response.status_code != 200:
                print(f"\n  attempt {attempt}: HTTP {response.status_code}: {response.text[:400]}")
                counts["HTTP_ERROR"] = counts.get("HTTP_ERROR", 0) + 1
                continue

            body = response.json()
            choices = body.get("choices") or []
            if not choices:
                print(f"\n  attempt {attempt}: no choices in the response")
                counts["NO_CHOICES"] = counts.get("NO_CHOICES", 0) + 1
                continue

            verdict, facts = classify(choices[0])
            counts[verdict] = counts.get(verdict, 0) + 1
            usage = body.get("usage") or {}
            completion_tokens = int(usage.get("completion_tokens") or 0)
            tokens.append(completion_tokens)
            if facts.get("finish_reason") == "length":
                truncated.append(attempt)
            print(f"\n  attempt {attempt}: {verdict}")
            print(f"    {json.dumps(facts, ensure_ascii=False)}")
            print(f"    usage: prompt={usage.get('prompt_tokens')} completion={completion_tokens}")

            content = (choices[0].get("message") or {}).get("content") or ""
            message = choices[0].get("message") or {}
            # Both spellings, in the same order `classify` uses. Reading only `reasoning_content`
            # silently printed nothing against vLLM 0.28.0, whose openai_gptoss parser names the
            # analysis channel `reasoning` — so the dump this step exists for produced no output.
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if content.strip():
                print(f"    content preview: {content.strip()[:200]!r}")
            if reasoning:
                if args.dump_reasoning:
                    # Printed in full and *not* summarised: the Step 3 verdict turns on whether this
                    # text is the model's answer or its analysis, and a 200-char preview is exactly
                    # the thing that would hide the difference.
                    print(f"    reasoning (full, {len(str(reasoning))} chars):")
                    print(f"      {str(reasoning).strip()!r}")
                else:
                    print(f"    reasoning preview: {str(reasoning).strip()[:200]!r}")

    if not counts:
        # Nothing was attempted. With --dump-prompt that is a deliberate inspection run; without it,
        # `--repeat 0` is a mistake worth a non-zero exit rather than a silent success.
        print("\nno attempts were made (--repeat 0)")
        return 0 if args.dump_prompt else 2

    print("\nsummary")
    for verdict, count in sorted(counts.items()):
        print(f"  {verdict:<18} {count}")
    if tokens:
        ordered = sorted(tokens)

        def percentile(fraction: float) -> int:
            """Nearest-rank percentile; exact for the small n a probe uses."""
            index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
            return ordered[index]

        # The budget the REQUEST used, which is `--max-tokens` when given and the node's own default
        # otherwise. Reporting the node default here made a 4096 run print `budget=2048` and judge its
        # headroom against a number it had not used — a reporting bug that invalidated the comparison.
        budget = max_tokens
        default = NODE_MAX_TOKENS[args.node]
        headroom = (1 - max(ordered) / budget) * 100 if budget else 0.0
        print(
            f"  variant={variant}  node={args.node}  max_tokens={budget} (node default {default})"
        )
        print(
            f"  completion tokens: n={len(ordered)} min={ordered[0]} "
            f"p50={percentile(0.5)} p90={percentile(0.9)} max={ordered[-1]} "
            f"budget={budget} headroom={headroom:.0f}%"
        )
        # The number the operator asked for: how close the worst case runs to the cap. A max inside
        # the budget is not the same as a max *comfortably* inside it, and a truncation is a budget
        # problem by definition — the generation was cut off before it finished.
        if truncated:
            print(
                f"  !! {len(truncated)} attempt(s) hit the {budget}-token cap "
                f"(attempts {truncated}) — the budget in use is too tight for this shape"
            )
        elif ordered[-1] >= budget * 0.9:
            print(
                f"  !! worst case {ordered[-1]} is within 10% of the {budget}-token budget; a longer "
                "answer would truncate"
            )
        else:
            print(f"  ok: worst case {ordered[-1]} leaves {headroom:.0f}% of the budget unused")

    print("\nreading")
    if counts.get("REASONING_ONLY"):
        print("  H1 is real: the text arrives in a reasoning channel the router does not read.")
        print("  A blind retry would reproduce it. Fix the channel or the request parameters.")
        if args.dump_reasoning:
            print("  Read the dump above and decide: is that text the ANSWER or the ANALYSIS?")
            print("  The Step 3 verdict depends on it, and it must be recorded either way.")
    if counts.get("LENGTH_TRUNCATED"):
        print("  H2 is real: generations are being cut off. Retry or raise the budget.")
    if counts.get("NO_CHANNELS") and not counts.get("REASONING_ONLY"):
        print(
            "  Neither channel carried text and it was not a truncation: suspect the served template"
        )
        print("  or the parser, and try --extra-json with the parameters that template documents.")

    return 0 if set(counts) == {"CONTENT_OK"} else 1


if __name__ == "__main__":
    sys.exit(main())
