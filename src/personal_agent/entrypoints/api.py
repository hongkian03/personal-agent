"""Authenticated HTTP access to the same conversational runtime used by the CLI."""

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from starlette.types import Receive, Scope, Send

from personal_agent.agent.progress import ChatEventBuffer
from personal_agent.agent.service import ChatValidationError, RuntimeConfigurationError
from personal_agent.bootstrap import open_runtime

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
STREAM_HEARTBEAT_SECONDS = 15


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
        except RuntimeConfigurationError:
            raise HTTPException(status_code=503, detail="Agent is not configured") from None
        except ChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Agent request timed out") from None
        except Exception as exc:
            # Avoid exposing provider responses, credentials, messages or tracebacks.
            logger.warning("chat_failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Agent request failed") from None
        return ChatResponse(
            conversation_id=result.conversation_id, run_id=result.run_id, text=result.text
        )

    @app.post("/chat/stream", dependencies=[Depends(authorize)])
    async def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        runtime = request.app.state.runtime
        try:
            runtime.validate_chat(payload.message, payload.conversation_id)
        except RuntimeConfigurationError:
            raise HTTPException(status_code=503, detail="Agent is not configured") from None
        except ChatValidationError as exc:
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
                    logger.warning("chat_stream_failed error_type=%s", type(exc).__name__)

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
            logger.warning("morning_generation_failed error_type=%s", type(exc).__name__)
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
            logger.warning("portfolio_refresh_failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Portfolio refresh unavailable") from None

    @app.post("/portfolio/morning/claim", response_model=None, dependencies=[Depends(authorize)])
    async def claim_portfolio_morning(request: Request) -> Response | dict[str, str]:
        morning = request.app.state.runtime.portfolio_morning
        if morning is None:
            return Response(status_code=204)
        try:
            text = await morning.claim_due()
        except Exception as exc:
            logger.warning("portfolio_briefing_failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Portfolio briefing unavailable") from None
        return Response(status_code=204) if text is None else {"text": text}

    return app
