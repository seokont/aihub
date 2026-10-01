"""The cloud provider: one protocol, one OpenAI-compatible implementation.

CLAUDE.md §4 fixes the shape — "cloud LLM: behind a ``CloudProvider`` interface; concrete
provider configured by env" — and this module is that interface plus the single implementation
this phase needs. Every provider this system will ever have speaks the OpenAI chat-completions
protocol, because the local vLLM already does and ``moni_router.wire`` is the one place that
protocol is written.

**What is deliberately *not* here: the decision.** A provider knows how to reach an endpoint; it
does not know whether it *may*. That question belongs to :mod:`moni_router.policy`, which is the
only module allowed to build one of these (see the guard test
``tests/unit/router/test_cloud_gate.py``). A provider that decided its own eligibility would be a
second policy, and the first bug in either would be a leak.

**One attempt per call, and that is a security property rather than a simplification.** Retrying a
cloud request would make "how many times did this payload leave the server?" a number the audit
cannot state. The router's answer to a failed cloud call is to degrade to the local model (§3.12),
never to re-send; so the count stays exactly one per routed call, which is what the canary test
asserts and what the audit row reports.

**The failure body is read, but only its machine-readable code is kept.** The local path logs vLLM's
error body because it carries a parser traceback that is otherwise undiagnosable. A cloud endpoint's
error body can echo the request — which is the payload we just sent — so the body itself is never
logged or raised (§3.11: no user content in logs, and §3.4 makes some of that payload level A). What
*is* kept is a bounded discriminator from the API's own error-code field, extracted by
:mod:`moni_router.diagnostics`, which reads an allowlist of code paths and refuses anything that is
not token-shaped. That distinction is the difference between ``cloud model error (404)`` — which is
what a retired model name and a dead endpoint both produced — and
``cloud model error (404: model_not_found)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
import structlog

from moni_router.diagnostics import reason_from_response, status_and_reason
from moni_router.models import ChatResult, StreamChunk, ToolSpec
from moni_router.wire import (
    DEFAULT_TIMEOUT_SECONDS,
    ChatPayloadError,
    ModelUnavailable,
    build_payload,
    parse_completion,
    parse_stream_lines,
)

log = structlog.get_logger(__name__)

#: The only provider implementation this phase ships. Named in ``CLOUD_PROVIDER``; any other value
#: is refused rather than silently treated as OpenAI-compatible, because "we assumed your provider
#: speaks the same protocol" is exactly the assumption that would send data somewhere unexpected.
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset({"openai"})

CHAT_COMPLETIONS_PATH: Final = "/chat/completions"


class CloudUnavailable(ModelUnavailable):
    """The cloud endpoint is unconfigured, unreachable or not answering usefully.

    A subclass of :class:`~moni_router.wire.ModelUnavailable` so a caller can treat "no model" as
    one condition, while the router's degradation path can catch it precisely.
    """


class CloudMisconfigured(RuntimeError):
    """The cloud settings are present but unusable.

    Raised before anything is sent. A half-configured egress path is a mistake no request should
    paper over: it looks enabled and is not.
    """


@dataclass(frozen=True, slots=True)
class CloudConfig:
    """The cloud settings, as values.

    Deliberately not read from the environment here. ``CLOUD_*`` is declared in the gateway's
    :class:`~moni_gateway.config.Settings` — the same way ``VLLM_*`` reaches the router — so that
    ``scripts/check_environment.py`` keeps enforcing that every setting the stack reads is
    documented in ``.env.example``. A router reading its own environment escapes that audit
    entirely, and this object is what the composition root fills in instead.

    ``client`` is the transport seam: a test injects one bound to ``httpx.MockTransport`` and then
    the *real* provider builds and sends the real request, which is how the canary test records
    what actually left the process rather than what a stub was told to pretend.
    """

    base_url: str
    api_key: str
    model: str
    provider: str = "openai"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    client: httpx.AsyncClient | None = None


class CloudProvider(Protocol):
    """What the policy needs from a cloud endpoint: chat, and stream.

    Both are async and both take already-wire-shaped messages, so a provider performs no
    conversion of its own and the anonymiser's output is exactly what goes on the wire.
    """

    name: str

    async def chat(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> ChatResult: ...

    def stream(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]: ...


class OpenAICompatibleCloudProvider:
    """An OpenAI-compatible endpoint: ``POST {base_url}/chat/completions`` with a bearer token."""

    name = "openai"

    def __init__(self, config: CloudConfig) -> None:
        self._config = config
        self._base_url = config.base_url.rstrip("/")

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        """The client to use, and whether this call owns (and must close) it."""
        injected = self._config.client
        if injected is not None:
            return injected, False
        return (
            httpx.AsyncClient(base_url=self._base_url, timeout=self._config.timeout_seconds),
            True,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> ChatResult:
        body = build_payload(
            [dict(message) for message in messages],
            tools,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        client, owns = self._client()
        try:
            try:
                response = await client.post(
                    CHAT_COMPLETIONS_PATH, json=body, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                msg = f"cloud model unreachable at {self._base_url}"
                raise CloudUnavailable(msg) from exc
            if response.status_code >= 400:
                # Status **and** a bounded reason. The reason is drawn only from the API's own
                # machine-readable code field and must be token-shaped, so it can never be the
                # request echoed back — see `moni_router.diagnostics` for why the prose next to it
                # is deliberately not read. Before this, a retired model and an outage were the same
                # log line (`cloud model error (404)`), which is how a misconfigured model name
                # survived a "the key works" check.
                reason = reason_from_response(response)
                msg = f"cloud model error ({status_and_reason(response.status_code, reason)})"
                raise CloudUnavailable(msg)
            try:
                payload = response.json()
            except ValueError as exc:
                msg = "cloud model returned a non-JSON response"
                raise CloudUnavailable(msg) from exc
            try:
                return parse_completion(payload, fallback_model=model)
            except ChatPayloadError as exc:
                # A 200 with nothing readable in it is as unusable as an outage, and the router's
                # answer to both is the same: run this step locally.
                msg = "cloud model returned no usable completion"
                raise CloudUnavailable(msg) from exc
        finally:
            if owns:
                await client.aclose()

    async def stream(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]:
        body = build_payload(
            [dict(message) for message in messages],
            tools,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        client, owns = self._client()
        stream = None
        try:
            try:
                stream = client.stream(
                    "POST", CHAT_COMPLETIONS_PATH, json=body, headers=self._headers()
                )
                response = await stream.__aenter__()
            except httpx.HTTPError as exc:
                msg = f"cloud model unreachable at {self._base_url}"
                raise CloudUnavailable(msg) from exc
            if response.status_code >= 400:
                # Clear the body before it is discarded: a stream that is abandoned unread makes the
                # reason unavailable, and the reason is the only thing that distinguishes a rejected
                # model from an outage. Read, then bounded by the same helper as the non-streaming
                # path — so the two cannot drift into reporting different amounts.
                try:
                    await response.aread()
                except (
                    httpx.HTTPError
                ):  # pragma: no cover - a body we cannot read is a status-only reason
                    pass
                reason = reason_from_response(response)
                msg = f"cloud model error ({status_and_reason(response.status_code, reason)})"
                raise CloudUnavailable(msg)
            async for chunk in parse_stream_lines(response.aiter_lines()):
                yield chunk
        finally:
            if stream is not None:
                await stream.__aexit__(None, None, None)
            if owns:
                await client.aclose()


def provider_from_config(config: CloudConfig | None) -> CloudProvider | None:
    """Build the provider the config asks for, or None when there is no cloud configured.

    None is a supported state, not an error: the stack runs local-only, and the router says so in
    the route's reason rather than failing a run (§3.12's degraded mode). A *partial* config is an
    error — see :class:`CloudMisconfigured`.
    """
    if config is None:
        return None
    if not config.base_url.strip() or not config.api_key.strip():
        msg = (
            "cloud configuration is incomplete: a base URL and an API key are both required, and "
            "an empty CLOUD_BASE_URL/CLOUD_API_KEY means 'no cloud' rather than 'a default'"
        )
        raise CloudMisconfigured(msg)
    provider = config.provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        msg = (
            f"unsupported cloud provider {config.provider!r}; this build implements "
            f"{sorted(SUPPORTED_PROVIDERS)}"
        )
        raise CloudMisconfigured(msg)
    return OpenAICompatibleCloudProvider(config)


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "SUPPORTED_PROVIDERS",
    "CloudConfig",
    "CloudMisconfigured",
    "CloudProvider",
    "CloudUnavailable",
    "OpenAICompatibleCloudProvider",
    "provider_from_config",
]
