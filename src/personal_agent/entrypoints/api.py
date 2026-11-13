"""Authenticated HTTP access to the same conversational runtime used by the CLI."""

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from traceback import extract_tb
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from starlette.types import Receive, Scope, Send

from personal_agent.agent.models import ModelError
from personal_agent.agent.progress import ChatEventBuffer
from personal_agent.agent.service import ChatValidationError, RuntimeConfigurationError
from personal_agent.agent.tools import ToolOutcomeUnknown
from personal_agent.bootstrap import open_runtime
from personal_agent.integrations.cognee import CogneeError

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
STREAM_HEARTBEAT_SECONDS = 15


def log_failure(
    event: str,
    exc: Exception,
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Log application-authored explanations and locations without provider payloads."""
    if isinstance(exc, (
        ModelError, ChatValidationError, RuntimeConfigurationError, ToolOutcomeUnknown,
    )):
        # These exceptions contain application-authored messages. Provider exception
        # strings and tracebacks can contain prompts, response bodies, or credentials.
        message = str(exc)
    elif isinstance(exc, TimeoutError):
        message = "Request exceeded its execution deadline."
    else:
        message = "Unexpected application error; inspect error_type, cause_type, and source."
    cause = exc.__cause__
    if cause is None and not exc.__suppress_context__:
        cause = exc.__context__

    def location(error: BaseException | None) -> str:
        frames = extract_tb(error.__traceback__) if error is not None else []
        return f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "-"

    details = {}
    if isinstance(exc, ModelError):
        details = {
            name: getattr(exc, name) for name in (
                "code", "retryable", "response_status", "incomplete_reason",
                "provider_request_id", "response_id", "http_status", "hosted_tool_calls",
            ) if getattr(exc, name) is not None
        }
    logger.warning(
        "%s error_type=%s message=%s cause_type=%s source=%s cause_source=%s "
        "run_id=%s conversation_id=%s details=%s",
        event,
        type(exc).__name__,
        json.dumps(message[:500], ensure_ascii=False),
        type(cause).__name__ if cause is not None else "-",
        location(exc),
        location(cause),
        run_id or "-",
        conversation_id or "-",
        json.dumps(details, ensure_ascii=False),
    )


class ChatStreamingResponse(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # ASGI 2.4 send errors occur outside the suspended iterator. Close it
            # explicitly rather than waiting for garbage collection to cancel work.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    conversation_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank")
        return value


class ChatResponse(BaseModel):
    conversation_id: str
    run_id: str
    text: str


async def authorize(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    configured = request.app.state.runtime.settings.api_token
    if configured is None or not configured.get_secret_value():
        raise HTTPException(status_code=503, detail="API access is not configured")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_runtime() as runtime:
            app.state.runtime = runtime
            yield

    app = FastAPI(title="Personal Agent", version="0.1.0", lifespan=lifespan)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        # Process liveness only; this does not assert model or integration readiness.
        return {"status": "ok"}

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(authorize)])
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        try:
            result = await request.app.state.runtime.chat(
                message=payload.message, conversation_id=payload.conversation_id
            )
        except RuntimeConfigurationError as exc:
            log_failure("chat_failed", exc, conversation_id=payload.conversation_id)
            raise HTTPException(status_code=503, detail="Agent is not configured") from None
        except ChatValidationError as exc:
            log_failure("chat_failed", exc, conversation_id=payload.conversation_id)
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except TimeoutError as exc:
            log_failure("chat_failed", exc, conversation_id=payload.conversation_id)
            raise HTTPException(status_code=504, detail="Agent request timed out") from None
        except Exception as exc:
            log_failure("chat_failed", exc, conversation_id=payload.conversation_id)
            raise HTTPException(status_code=502, detail="Agent request failed") from None
        return ChatResponse(
            conversation_id=result.conversation_id, run_id=result.run_id, text=result.text
        )

    @app.get("/memory/graph", dependencies=[Depends(authorize)])
    async def memory_graph(request: Request, full: bool = False) -> Response:
        runtime = request.app.state.runtime
        if runtime.memory is None:
            raise HTTPException(status_code=503, detail="Long-term memory is not configured")
        try:
            html = await runtime.memory.graph_snapshot(
                owner_id=runtime.settings.owner_id, full=full,
            )
        except CogneeError:
            raise HTTPException(status_code=502, detail="Memory graph export unavailable") from None
        return Response(content=html, media_type="text/html", headers={
            "Content-Disposition": 'attachment; filename="memory-graph.html"',
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox allow-scripts",
        })

    @app.post("/chat/stream", dependencies=[Depends(authorize)])
    async def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        runtime = request.app.state.runtime
        try:
            runtime.validate_chat(payload.message, payload.conversation_id)
        except RuntimeConfigurationError as exc:
            log_failure("chat_stream_rejected", exc, conversation_id=payload.conversation_id)
            raise HTTPException(status_code=503, detail="Agent is not configured") from None
        except ChatValidationError as exc:
            log_failure("chat_stream_rejected", exc, conversation_id=payload.conversation_id)
            raise HTTPException(status_code=400, detail=str(exc)) from None

        async def events():
            buffer = ChatEventBuffer()

            async def produce():
                try:
                    await runtime.chat(
                        message=payload.message,
                        conversation_id=payload.conversation_id,
                        on_event=buffer.put,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Runtime emits a safe terminal error before re-raising.
                    failure = buffer.terminal or buffer.started
                    log_failure(
                        "chat_stream_failed", exc,
                        run_id=failure.run_id if failure is not None else None,
                        conversation_id=(
                            failure.conversation_id if failure is not None
                            else payload.conversation_id
                        ),
                    )

            task = asyncio.create_task(produce())
            try:
                while True:
                    try:
                        async with asyncio.timeout(STREAM_HEARTBEAT_SECONDS):
                            event = await buffer.get()
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    data = json.dumps(event.payload(), ensure_ascii=False)
                    yield f"id: {event.sequence}\nevent: {event.kind}\ndata: {data}\n\n"
                    if event.kind in {"result", "error"}:
                        break
            finally:
                # Shield cleanup from Starlette's disconnect cancellation scope.
                with anyio.CancelScope(shield=True):
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        return ChatStreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/morning/claim", response_model=None, dependencies=[Depends(authorize)])
    async def claim_morning(request: Request) -> Response | dict[str, str]:
        morning = request.app.state.runtime.morning
        if morning is None:
            raise HTTPException(status_code=503, detail="Agent is not configured")
        try:
            text = await morning.claim_due()
        except Exception as exc:
            log_failure("morning_generation_failed", exc)
            raise HTTPException(status_code=502, detail="Morning message unavailable") from None
        return Response(status_code=204) if text is None else {"text": text}

    @app.get("/portfolio/status", dependencies=[Depends(authorize)])
    async def portfolio_status(request: Request) -> dict:
        runtime = request.app.state.runtime
        return await runtime.portfolio.get_status(owner_id=runtime.settings.owner_id)

    @app.post("/portfolio/sync", dependencies=[Depends(authorize)])
    async def portfolio_sync(request: Request) -> dict:
        # Refresh is outside model tool execution: broker report generation can take minutes.
        try:
            return await request.app.state.runtime.portfolio.sync(force=True)
        except Exception as exc:
            log_failure("portfolio_refresh_failed", exc)
            raise HTTPException(status_code=502, detail="Portfolio refresh unavailable") from None

    @app.post("/portfolio/morning/claim", response_model=None, dependencies=[Depends(authorize)])
    async def claim_portfolio_morning(request: Request) -> Response | dict[str, str]:
        morning = request.app.state.runtime.portfolio_morning
        if morning is None:
            return Response(status_code=204)
        try:
            text = await morning.claim_due()
        except Exception as exc:
            log_failure("portfolio_briefing_failed", exc)
            raise HTTPException(status_code=502, detail="Portfolio briefing unavailable") from None
        return Response(status_code=204) if text is None else {"text": text}

    return app
