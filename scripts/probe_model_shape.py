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


def prompt_text(name: str) -> str:
    path = PROMPTS / f"{name}.md"
    if not path.is_file():
        raise SystemExit(f"prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def build_messages(variant: str, node: str, evidence: str) -> list[dict[str, str]]:
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
        sent: list[dict[str, str]] = payload["messages"]  # type: ignore[assignment]
        for index, message in enumerate(sent):
            body = message.get("content", "")
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
            print(f"\n  attempt {attempt}: {verdict}")
            print(f"    {json.dumps(facts, ensure_ascii=False)}")
            print(f"    usage: prompt={usage.get('prompt_tokens')} completion={completion_tokens}")

            content = (choices[0].get("message") or {}).get("content") or ""
            reasoning = (choices[0].get("message") or {}).get("reasoning_content") or ""
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
        print(f"  completion tokens: min={min(tokens)} max={max(tokens)}")

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
