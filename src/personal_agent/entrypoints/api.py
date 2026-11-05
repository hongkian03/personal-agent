"""Authenticated HTTP access to the same conversational runtime used by the CLI."""

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from personal_agent.agent.service import ChatValidationError, RuntimeConfigurationError
from personal_agent.bootstrap import open_runtime

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


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

    return app
