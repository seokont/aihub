"""The live proof that a chat run is traced, and that a level-C chat reaches the cloud (F18, F17).

These are the two acceptance tests the audit could not run. They are **not** a substitute for the unit
suites; they are the only place the claim "a chat-driven run appears in Langfuse" can be checked at
all, because the claim is about what leaves the process over a network to a third-party backend.

The gap that made both defects invisible
----------------------------------------
F18's defect was that no *interactive* run was traced while the tracing code and its tests were
perfectly correct — `tests/integration/agent/test_langfuse_live.py` builds its **own** tracer with
`tracer_from_env()`, so it stayed green in the broken state. F17's was the same shape: the router
suites pass a declared `user_text` part by hand, so they stayed green while the agent declared nothing.

Both tests below therefore drive ``/v1/chat/completions`` through nginx and then ask Langfuse and
PostgreSQL what happened, rather than asking the code whether it *would* have traced. The gateway
returns the run's id as ``moni_trace_id``, which is what joins the two records.

Both need a **completed** run, so they skip when the local model is unreachable: a run whose first
model call fails produces no trace to read. The skip is honest rather than a pass — a green suite here
with the tunnel down would mean nothing.
"""

from __future__ import annotations

import re

import pytest

from .conftest import (
    GATEWAY_TRACE_PATTERN,
    fetch_generations,
    langfuse_get,
    model_reachable,
    odoo_reachable,
    psql,
    require_langfuse,
    require_stack,
    safe_literal,
    wait_for_trace,
)

pytestmark = pytest.mark.integration

CHAT_PATH = "/v1/chat/completions"

#: A generic question with no company data in it: level C by §3.4's `user_text_bare` rule, so it is
#: cloud-eligible. Deliberately dull — this is the question the phase file names as the example.
LEVEL_C_QUESTION = "Explain the difference between FIFO and LIFO in one sentence."


def _body(question: str) -> dict[str, object]:
    return {
        "model": "moni-gateway",
        "messages": [{"role": "user", "content": question}],
        "stream": False,
    }


def require_model() -> None:
    """Skip unless the local model answers **to the gateway**.

    No run completes without it, and no trace is produced by a run that dies at its first model call.
    Skipping is the honest outcome: "we could not run it" is not "it passed".

    The message names the check to run, because the failure has two very different causes on this
    stand: the tunnel is down (host `127.0.0.1:8001`), or the tunnel is up but the container's
    `host.docker.internal:18001` leg is not reaching it. Only the second one makes a chat request 502.
    """
    if not model_reachable():
        pytest.skip(
            "the gateway cannot reach the local model, so no chat run can complete and no trace can "
            "exist. Check the container's own view: "
            "docker compose --env-file .env -f infra/docker-compose.dev.yml exec -T gateway python -c "
            "\"import os,httpx;print(httpx.get(os.environ['VLLM_BASE_URL'].rstrip('/')+'/models')"
            ".status_code)\" — the host's 127.0.0.1:8001 is an SSH forward the container cannot use"
        )


async def _run_chat(stack: object, question: str) -> str:
    """One chat run, returning its ``trace_id``."""
    token = await stack.token()  # type: ignore[attr-defined]
    response = await stack.nginx.post(  # type: ignore[attr-defined]
        CHAT_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json=_body(question),
    )
    if response.status_code != 200:
        pytest.fail(f"the chat run did not complete: {response.status_code} {response.text[:400]}")

    body = response.json()
    trace_id = body.get("moni_trace_id")
    assert isinstance(trace_id, str) and trace_id, (
        f"the gateway returned no moni_trace_id, so no run can be joined to its trace: {body}"
    )
    safe_literal(trace_id, GATEWAY_TRACE_PATTERN, "gateway trace id")
    return trace_id


def _destinations(trace_id: str) -> list[str]:
    return [
        str(row["metadata"]["destination"])
        for row in fetch_generations(trace_id)
        if isinstance(row.get("metadata"), dict) and "destination" in row["metadata"]
    ]


def _levels(trace_id: str) -> list[str]:
    return [
        str(row["metadata"]["data_level"])
        for row in fetch_generations(trace_id)
        if isinstance(row.get("metadata"), dict) and "data_level" in row["metadata"]
    ]


# ---------------------------------------------------------------------------
# F18 — an interactive run is traced
# ---------------------------------------------------------------------------


async def test_a_chat_run_appears_in_langfuse_under_its_own_trace_id(stack: object) -> None:
    """The claim: drive chat, get a ``run-*`` trace carrying this run's id.

    Fails in the pre-F18 state, and it fails *at the trace lookup* — which is the point. Everything
    upstream of Langfuse was already correct, so no assertion about the tracer could have caught it.
    """
    require_stack()
    require_langfuse()
    require_model()

    trace_id = await _run_chat(stack, LEVEL_C_QUESTION)
    trace = wait_for_trace(trace_id)

    assert trace is not None, (
        f"no Langfuse trace for the run's own trace id {trace_id}: the gateway did not trace this "
        "interactive run (F18). Check that the lifespan installs `app.state.tracer_factory`."
    )
    assert trace.get("id") == trace_id
    assert trace_id.startswith("run-"), "the gateway's own trace ids are run-* prefixed"


async def test_the_run_spans_carry_level_destination_and_anonymisation(stack: object) -> None:
    """The per-step facts must be on the generations, not only in the audit row.

    Both records matter and they are written from different code, so this asserts the trace side
    rather than letting the audit row's presence imply it.
    """
    require_stack()
    require_langfuse()
    require_model()

    trace_id = await _run_chat(stack, LEVEL_C_QUESTION)
    assert wait_for_trace(trace_id) is not None

    generations = fetch_generations(trace_id)
    assert generations, f"the trace for {trace_id} carries no generation spans"

    with_facts = [row for row in generations if isinstance(row.get("metadata"), dict)]
    assert with_facts, f"no generation carries metadata at all: {generations}"

    for row in with_facts:
        metadata = row["metadata"]
        assert metadata.get("destination") in {"local", "cloud"}, metadata
        assert metadata.get("data_level") in {"A", "B", "C"}, metadata
        assert isinstance(metadata.get("anonymized"), bool), metadata


async def test_the_audit_row_and_the_trace_agree_about_the_destination(stack: object) -> None:
    """One run, two records, one answer.

    They are written from different places — the gateway from the returned state, the tracer from
    inside the loop — so a disagreement is a real class of bug rather than a cosmetic one.
    """
    require_stack()
    require_langfuse()
    require_model()

    trace_id = await _run_chat(stack, LEVEL_C_QUESTION)
    assert wait_for_trace(trace_id) is not None

    row = psql(
        "SELECT result || '|' || coalesce(args_redacted::text, '') FROM audit_log "
        f"WHERE trace_id = '{trace_id}' AND action = 'agent.run';"
    )
    assert row.strip(), f"no agent.run audit row for trace {trace_id}"

    destinations = _destinations(trace_id)
    assert destinations, f"the trace for {trace_id} carries no destination at all"

    for destination in set(destinations):
        assert destination in row, (
            f"the trace says a call went {destination} but the audit row never mentions it: {row[:300]}"
        )


# ---------------------------------------------------------------------------
# F17 — the level-C / level-A pair through chat
# ---------------------------------------------------------------------------


async def test_a_level_c_chat_reaches_the_cloud(stack: object) -> None:
    """F17's acceptance, positive half: a generic question's call goes to the cloud.

    Asserted on the **trace**, and the audit row is checked in the test above, because the pre-F17
    failure was invisible in the audit row: the row was written, the level was just A.
    """
    require_stack()
    require_langfuse()
    require_model()

    trace_id = await _run_chat(stack, LEVEL_C_QUESTION)
    assert wait_for_trace(trace_id) is not None

    destinations = _destinations(trace_id)
    assert "cloud" in destinations, (
        f"a bare level-C question never reached the cloud: destinations on the run were "
        f"{destinations}. The agent must declare the user's question (F17), or the router fails "
        "closed to A and pins every first call to the local model."
    )


async def test_the_run_was_classified_rather_than_defaulted_to_a(stack: object) -> None:
    """The mechanism, read from the run's own record rather than from the code.

    ``declared_context`` including the question is what makes the composed level C. This asserts the
    observable consequence: at least one generation is level B or C, i.e. the run's context was
    actually *classified*.
    """
    require_stack()
    require_langfuse()
    require_model()

    trace_id = await _run_chat(stack, LEVEL_C_QUESTION)
    assert wait_for_trace(trace_id) is not None

    levels = _levels(trace_id)
    assert levels, f"no generation on {trace_id} carries a data level"
    assert set(levels) & {"B", "C"}, (
        f"every call in the run composed to A ({levels}), so the agent declared no user text and the "
        "router failed closed (F17)"
    )


async def test_a_level_a_chat_produces_zero_cloud_generations(stack: object) -> None:
    """F17's acceptance, negative half — and the phase's core §3.4 property.

    The context carries Odoo contact data, which is level A, so the run must show **no** cloud
    generation. Skipped rather than failed when DEV Odoo is unreachable: the level-A context has to
    actually arrive for the claim to mean anything.
    """
    require_stack()
    require_langfuse()
    require_model()
    if not odoo_reachable():
        pytest.skip("the container cannot reach DEV Odoo, so no level-A context can be assembled")

    trace_id = await _run_chat(
        stack, "Знайди партнера за адресою клієнта й покажи його email та телефон."
    )
    assert wait_for_trace(trace_id) is not None

    cloud = [
        row
        for row in fetch_generations(trace_id)
        if isinstance(row.get("metadata"), dict) and row["metadata"].get("destination") == "cloud"
    ]
    assert cloud == [], (
        "a level-A context produced cloud generations, which is the leak §3.4 exists to prevent: "
        f"{[row.get('metadata') for row in cloud]}"
    )


# ---------------------------------------------------------------------------
# Anti-vacuity for the absence assertions above
# ---------------------------------------------------------------------------


def test_the_langfuse_read_path_itself_works() -> None:
    """The tests above assert absence and membership in lists that an infrastructure fault could empty.

    If the read path were broken, ``fetch_generations`` would return ``[]`` and the level-A test would
    pass while proving nothing. So the API is exercised on its own and the response shape asserted.
    """
    require_stack()
    require_langfuse()

    response = langfuse_get("/traces", limit=5)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "data" in payload, f"unexpected Langfuse traces payload: {sorted(payload)}"

    ids = [str(row.get("id", "")) for row in payload["data"]]
    assert all(re.match(r"(run|itest)-[0-9a-f-]+", trace) for trace in ids), (
        f"Langfuse answered but with unfamiliar trace ids, so this may be the wrong project: {ids}"
    )
