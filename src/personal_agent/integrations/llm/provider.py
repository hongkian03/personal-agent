"""OpenAI Responses API adapter; the graph owns conversation persistence."""

import asyncio
import logging
import re
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, OpenAIError

from personal_agent.agent.models import (
    ActivityCallback,
    ActivityEvent,
    ModelError,
    ModelReply,
    ToolCall,
)
from personal_agent.agent.tools import ToolDefinition
from personal_agent.integrations.llm.citations import render_cited_text

logger = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_TRANSIENT_CODES = frozenset({"server_error", "rate_limit_exceeded", "vector_store_timeout"})
_QUOTA_CODES = frozenset({
    "insufficient_quota", "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "billing_hard_limit_reached",
})


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


class OpenAIModel:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        max_output_tokens: int = 2048,
        web_search_context_size: str = "medium",
        max_attempts: int = 2,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        if not 1 <= max_attempts <= 3 or not 0 <= retry_delay_seconds <= 10:
            raise ValueError("Invalid model retry settings")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.web_search_context_size = web_search_context_size
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        # Retry policy lives here so SDK retries cannot multiply attempt budgets.
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    async def aclose(self) -> None:
        await self.client.close()

    async def respond(
        self,
        *,
        items: list[dict[str, Any]],
        instructions: str,
        tools: tuple[ToolDefinition, ...],
        hosted_tools: frozenset[str] = frozenset(),
        max_hosted_tool_calls: int = 3,
        on_activity: ActivityCallback | None = None,
    ) -> ModelReply:
        if hosted_tools - {"web_search"}:
            raise ModelError("Unsupported hosted tool requested", code="invalid_configuration")
        functions: list[dict[str, Any]] = [
            {
                "type": "function",
                "name": definition.model_name,
                "description": definition.description,
                "parameters": definition.input_schema,
                "strict": False,
            }
            for definition in tools
        ]
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": items,
            "tools": functions,
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "parallel_tool_calls": False,
        }
        if "web_search" in hosted_tools:
            if max_hosted_tool_calls < 1:
                raise ModelError(
                    "Hosted web search requires a positive call limit", code="invalid_configuration"
                )
            functions.append(
                {"type": "web_search", "search_context_size": self.web_search_context_size}
            )
            request["max_tool_calls"] = max_hosted_tool_calls
        last_phase: str | None = None

        def emit(phase: str) -> None:
            nonlocal last_phase
            if phase == last_phase:
                return
            last_phase = phase
            if on_activity is not None:
                try:
                    on_activity(ActivityEvent(phase))
                except Exception:
                    # Optional progress delivery must not prevent a completed reply.
                    logger.warning("model_activity_callback_failed")

        # One wall deadline covers every attempt and all backoff. A retry repeats only
        # the current model request, and never replays application function tools.
        observed = {"hosted_tool_calls": 0}
        try:
            async with asyncio.timeout(self.timeout_seconds):
                for attempt in range(self.max_attempts):
                    emit("model_working")
                    try:
                        response = await self._attempt(request, emit, observed)
                        break
                    except ModelError as exc:
                        if (not exc.retryable or exc.hosted_tool_calls
                                or attempt + 1 >= self.max_attempts):
                            raise
                        logger.info(
                            "model_retry code=%s next_attempt=%s max_attempts=%s",
                            exc.code, attempt + 2, self.max_attempts,
                        )
                        emit("retrying")
                        await asyncio.sleep(self.retry_delay_seconds * (2 ** attempt))
        except (TimeoutError, asyncio.CancelledError) as exc:
            # Keep the established timeout contract while retaining conservative
            # hosted usage for resume after a model/chat deadline or disconnect.
            exc.hosted_tool_calls = observed["hosted_tool_calls"]
            raise
        try:
            return self._reply(response)
        except ModelError as exc:
            exc.response_id = _identifier(getattr(response, "id", None))
            exc.response_status = "completed"
            exc.hosted_tool_calls = max(
                exc.hosted_tool_calls, sum(
                    getattr(item, "type", None) == "web_search_call"
                    for item in (getattr(response, "output", None) or [])
                ),
            )
            raise
        except Exception as exc:
            raise ModelError(
                "OpenAI completed a response that could not be processed.",
                code="invalid_response", response_id=_identifier(getattr(response, "id", None)),
                hosted_tool_calls=sum(
                    getattr(item, "type", None) == "web_search_call"
                    for item in (getattr(response, "output", None) or [])
                ),
            ) from exc

    async def _attempt(self, request: dict[str, Any], emit, observed: dict[str, int]) -> Any:
        hosted_ids: set[str] = set()
        request_id = None
        response = None

        def failure(
            message: str, *, code: str, retryable: bool = False,
            response_status: str | None = None, incomplete_reason: str | None = None,
            http_status: int | None = None,
        ) -> ModelError:
            returned_hosted = sum(
                getattr(item, "type", None) == "web_search_call"
                for item in (getattr(response, "output", None) or [])
            )
            observed["hosted_tool_calls"] = max(
                observed["hosted_tool_calls"], len(hosted_ids), returned_hosted
            )
            return ModelError(
                message, code=code, retryable=retryable,
                response_status=response_status, incomplete_reason=incomplete_reason,
                provider_request_id=request_id,
                response_id=_identifier(getattr(response, "id", None)),
                http_status=http_status,
                hosted_tool_calls=observed["hosted_tool_calls"],
            )

        def response_failure() -> ModelError:
            status = getattr(response, "status", None)
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            safe_reason = reason if reason in {
                "max_output_tokens", "max_messages", "content_filter", "steered"
            } else None
            if status == "incomplete" and safe_reason == "max_output_tokens":
                return failure(
                    f"OpenAI reached the output limit of {self.max_output_tokens} tokens "
                    "(including reasoning). Increase PERSONAL_AGENT_MAX_OUTPUT_TOKENS "
                    "or narrow the request, then retry.",
                    code="output_limit", response_status="incomplete",
                    incomplete_reason="max_output_tokens",
                )
            if status == "incomplete":
                return failure(
                    "OpenAI did not complete the response. "
                    f"Incomplete reason: {safe_reason or 'not supplied'}.",
                    code="incomplete_response", response_status="incomplete",
                    incomplete_reason=safe_reason,
                )
            provider_code = getattr(getattr(response, "error", None), "code", None)
            return failure(
                "OpenAI reported a failed response.", code="provider_failed",
                retryable=provider_code in _TRANSIENT_CODES,
                response_status=status if status in {"failed", "cancelled"} else None,
            )

        try:
            async with self.client.responses.stream(**request) as stream:
                # The high-level SDK accumulator does not expose HTTP headers in
                # every version; capture a request ID only when publicly available.
                http_response = getattr(stream, "response", None)
                if http_response is not None:
                    request_id = _identifier(http_response.headers.get("x-request-id"))
                async for event in stream:
                    if getattr(event, "response", None) is not None:
                        response = event.response
                        observed["hosted_tool_calls"] = max(
                            observed["hosted_tool_calls"], len(hosted_ids), sum(
                                getattr(item, "type", None) == "web_search_call"
                                for item in (getattr(response, "output", None) or [])
                            ),
                        )
                    if event.type.startswith("response.web_search_call."):
                        # Count initiation even if the stream stops before completion.
                        hosted_ids.add(str(getattr(event, "item_id", "unknown")))
                        observed["hosted_tool_calls"] = max(
                            observed["hosted_tool_calls"], len(hosted_ids)
                        )
                        emit("search_completed" if event.type.endswith(".completed")
                             else "searching")
                    elif event.type == "response.output_item.added":
                        if getattr(event.item, "type", None) == "web_search_call":
                            hosted_ids.add(str(getattr(event.item, "id", "unknown")))
                            observed["hosted_tool_calls"] = max(
                                observed["hosted_tool_calls"], len(hosted_ids)
                            )
                    elif event.type in {
                        "response.output_text.delta", "response.refusal.delta",
                    } and event.delta:
                        emit("response_started")
                    elif event.type in {"response.failed", "response.incomplete"}:
                        raise response_failure()
                    elif event.type == "error":
                        provider_code = getattr(event, "code", None)
                        raise failure(
                            "OpenAI reported an error while generating the response.",
                            code="provider_failed", retryable=provider_code in _TRANSIENT_CODES,
                        )
                response = await stream.get_final_response()
                if response.status != "completed":
                    raise response_failure()
                return response
        except ModelError:
            raise
        except APIStatusError as exc:
            request_id = _identifier(exc.request_id) or request_id
            quota = getattr(exc, "code", None) in _QUOTA_CODES
            transient = exc.status_code in {408, 409, 429} or exc.status_code >= 500
            raise failure(
                "OpenAI rejected the request with HTTP "
                f"{exc.status_code}. " + (
                    "Check the account's credits and spending limits." if quota else
                    "Check credentials and request configuration, or retry a temporary failure."
                ),
                code="quota_exceeded" if quota else (
                    "rate_limited" if exc.status_code == 429 else "provider_http_error"
                ),
                retryable=transient and not quota, http_status=exc.status_code,
            ) from exc
        except (APIConnectionError, httpx.TransportError) as exc:
            raise failure(
                "The connection to OpenAI failed before a complete response was received.",
                code="provider_unavailable", retryable=True,
            ) from exc
        except OpenAIError as exc:
            provider_code = getattr(exc, "code", None)
            quota = provider_code in _QUOTA_CODES
            raise failure(
                "OpenAI could not produce a usable response; "
                "check model and request configuration.",
                code="quota_exceeded" if quota else "provider_error",
                retryable=provider_code in _TRANSIENT_CODES and not quota,
            ) from exc
        except Exception as exc:
            raise failure(
                "OpenAI stream ended without a completed response or could not be processed.",
                code="invalid_stream",
            ) from exc

    def _reply(self, response: Any) -> ModelReply:
        # Preserve reasoning and function-call items for subsequent requests, as required
        # by the Responses API. Never reconstruct history from output_text alone.
        output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        calls = [
            ToolCall(call_id=item.call_id, name=item.name, arguments=item.arguments)
            for item in response.output
            if item.type == "function_call"
        ]
        if not output:
            raise ModelError("OpenAI returned an empty response", code="empty_response")
        text = render_cited_text(output)
        if not text.strip() and not calls:
            text = "\n".join(
                part.refusal
                for item in response.output
                if item.type == "message"
                for part in item.content
                if part.type == "refusal"
            )
            if not text.strip():
                raise ModelError(
                    "OpenAI returned no visible reply or tool calls", code="empty_response"
                )
        return ModelReply(
            items=output,
            text=text,
            tool_calls=calls,
            hosted_tool_calls=sum(item.get("type") == "web_search_call" for item in output),
        )
