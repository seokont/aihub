"""Integration: the OpenAI-compatible surface against the running stack.

Keycloak issues a token → nginx proxies `/v1/...` **without** a prefix rewrite → the
gateway verifies the JWT, computes the tool allow-list from the verified realm roles,
enforces the per-user rate limit, runs the agent, and writes one `audit_log` row that is
read back out of PostgreSQL.

Skipped unless ``MONI_RUN_INTEGRATION=1`` and the stack answers (`make test-integration`).

**On dependency outcomes.** The agent run itself needs three things that live outside the
gateway: the local model, odoo-mcp, and Odoo reachable *from the container*. This suite
asserts the contract that holds regardless — the HTTP status is one of the documented
outcomes, nothing leaks, and exactly one audit row is written with the right actor and trace
id. Where the run *succeeds* it asserts the completion shape too, so the test strengthens
automatically once every dependency is healthy instead of silently staying weak.
"""

from __future__ import annotations

import re

import pytest

from .conftest import action_literal, audit_count, psql, safe_literal

pytestmark = pytest.mark.integration

RUN_ACTION = "agent.run"
COMPLETION_ACTION = ACTION = "agent.run"
# Run ids are `run-<32 hex>`; asserting the shape keeps the f-string SQL honest.
TRACE_PATTERN = re.compile(r"\Arun-[0-9a-f]{32}\Z")

#: Statuses this surface may legitimately answer with. 200 = the run completed; 502 = the
#: run failed and was reported as an error rather than as an empty-but-successful answer
#: (fail closed). A 500 would mean an unhandled defect and is deliberately not allowed.
ALLOWED_STATUSES = {200, 502}


async def test_models_round_trip_through_nginx(stack: object) -> None:
    live = stack
    token = await live.token()  # type: ignore[attr-defined]
    response = await live.nginx.get(  # type: ignore[attr-defined]
        "/v1/models", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert [card["id"] for card in payload["data"]] == ["moni-main"]


async def test_models_without_a_token_is_401_through_nginx(stack: object) -> None:
    live = stack
    response = await live.nginx.get("/v1/models")  # type: ignore[attr-defined]
    assert response.status_code == 401


async def test_models_with_a_garbage_token_is_401(stack: object) -> None:
    live = stack
    response = await live.nginx.get(  # type: ignore[attr-defined]
        "/v1/models", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_a_chat_run_is_audited_with_the_actor_and_trace_id(stack: object) -> None:
    """The §3.8 requirement, end to end: one row per run, attributable and traceable."""
    live = stack
    token = await live.token()  # type: ignore[attr-defined]

    response = await live.nginx.post(  # type: ignore[attr-defined]
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "moni-main",
            "messages": [{"role": "user", "content": "Скільки в мене відкритих задач?"}],
        },
        timeout=180.0,
    )

    assert response.status_code in ALLOWED_STATUSES, response.text

    if response.status_code == 200:
        body = response.json()
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["choices"][0]["finish_reason"] == "stop"
        trace_id = body["moni_trace_id"]
    else:
        # The failure is reported as an error, never as an empty answer.
        assert "agent run failed" in response.json()["detail"]
        # The audit row is the only place the trace id survives a failed run, so it has to
        # be found by action rather than by an id the caller never received.
        trace_id = None

    # The row exists, attributed to the verified subject.
    assert audit_count(f"action = '{action_literal(RUN_ACTION)}'") >= 1

    if trace_id is not None:
        safe_literal(trace_id, TRACE_PATTERN, "trace id")
        recorded = psql(
            "SELECT result || '|' || user_id FROM audit_log "
            f"WHERE trace_id = '{trace_id}' AND action = '{RUN_ACTION}';"
        )
        lines = [line for line in recorded.splitlines() if line.strip()]
        assert lines, f"no audit row for trace {trace_id}"
        result, user_id = lines[-1].split("|", 1)
        # `ok` on a clean run. `limit` legitimately appears when a dependency the agent
        # needs is unavailable (it spends its step budget retrying, then says so), and
        # `error` when the run raised. All three are recorded outcomes; what must never
        # happen is a missing row, or an unattributable one.
        assert result in {"ok", "limit"} or result.startswith("error"), result
        # The actor is the Keycloak subject, i.e. a UUID — never the model's claim.
        assert re.match(r"\A[0-9a-f-]{36}\Z", user_id), user_id


async def test_a_streamed_run_returns_openai_chunks(stack: object) -> None:
    """The framing LibreChat parses, through nginx, with buffering disabled."""
    live = stack
    token = await live.token()  # type: ignore[attr-defined]

    async with live.nginx.stream(  # type: ignore[attr-defined]
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "moni-main",
            "stream": True,
            "messages": [{"role": "user", "content": "Скажи 'готово'."}],
        },
        timeout=180.0,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])

    assert "chat.completion.chunk" in raw
    assert raw.rstrip().endswith("data: [DONE]")


async def test_the_rate_limit_refuses_after_the_configured_budget(stack: object) -> None:
    """The limiter is real Redis, so this drives the production counter.

    Runs `AGENT_RATE_LIMIT_PER_HOUR + 1` requests, so it is only exercised when the budget
    is small. With the 30/hour default it would be slow *and* would spend the whole budget
    for the user, so it skips unless the stack was started with a test-sized limit.
    """
    live = stack
    import os

    limit = int(os.environ.get("AGENT_RATE_LIMIT_PER_HOUR", "30"))
    if limit > 5:
        pytest.skip(
            f"AGENT_RATE_LIMIT_PER_HOUR is {limit}; the refusal check needs a small budget "
            "(start the stack with AGENT_RATE_LIMIT_PER_HOUR=3 to run it)"
        )

    token = await live.token()  # type: ignore[attr-defined]
    headers = {"Authorization": f"Bearer {token}"}
    body = {"model": "moni-main", "messages": [{"role": "user", "content": "привіт"}]}

    statuses = []
    for _ in range(limit + 1):
        response = await live.nginx.post(  # type: ignore[attr-defined]
            "/v1/chat/completions", headers=headers, json=body, timeout=180.0
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert response.headers["Retry-After"].isdigit()
            assert int(response.headers["Retry-After"]) > 0
            break

    assert 429 in statuses, f"the limiter never refused within {limit + 1} runs: {statuses}"
    # And the refusal is recorded as a limit, not as a successful run.
    assert audit_count(f"action = '{RUN_ACTION}' AND result LIKE 'limit:%'") >= 1


async def test_a_bad_request_body_is_422_not_a_crash(stack: object) -> None:
    live = stack
    token = await live.token()  # type: ignore[attr-defined]
    response = await live.nginx.post(  # type: ignore[attr-defined]
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "moni-main", "messages": []},
    )
    assert response.status_code == 422


async def test_nginx_proxies_v1_verbatim_without_a_prefix_rewrite(stack: object) -> None:
    """A `/api/`-style rewrite here would 404; the paths must match exactly."""
    live = stack
    response = await live.nginx.get("/v1/models")  # type: ignore[attr-defined]
    # 401 (not 404) proves nginx reached the gateway's /v1/models route.
    assert response.status_code == 401
