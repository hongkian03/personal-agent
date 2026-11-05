"""Transport-independent conversation service for the single configured owner."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from personal_agent.config import Settings
from personal_agent.core.context import ExecutionContext
from personal_agent.skills.discovery import discover_instructions

logger = logging.getLogger(__name__)


class RuntimeConfigurationError(RuntimeError):
    """The runtime needs configuration before it can chat."""


class ChatValidationError(ValueError):
    """The caller should change the message or start a new conversation."""


@dataclass(frozen=True)
class ChatResult:
    conversation_id: str
    run_id: str
    text: str


class AgentRuntime:
    def __init__(self, *, settings: Settings, graph: Any, tools: Any, skills: Any) -> None:
        self.settings = settings
        self.graph = graph
        self.tools = tools
        self.skills = skills
        # Fixed stripes bound memory while serializing each conversation in one process.
        self._locks = [asyncio.Lock() for _ in range(64)]

    async def chat(self, message: str, conversation_id: str | None = None) -> ChatResult:
        if not message.strip() or len(message) > 20000:
            raise ChatValidationError("Message must contain 1–20000 characters.")
        conversation_id = conversation_id or uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
            raise ChatValidationError("Invalid conversation ID.")
        if self.graph is None:
            raise RuntimeConfigurationError("Set OPENAI_API_KEY or PERSONAL_AGENT_MODEL_API_KEY.")
        run_id = uuid4().hex
        # Prevent ambiguous owner/conversation concatenation and scope persisted checkpoints.
        thread_id = hashlib.sha256(
            json.dumps([self.settings.owner_id, conversation_id]).encode()
        ).hexdigest()
        context = ExecutionContext(
            owner_id=self.settings.owner_id,
            run_id=run_id,
            granted_scopes=frozenset({"skills:read", "skills:write", "runtime:read"}),
            conversation_id=conversation_id,
        )
        config = {
            "configurable": {"thread_id": thread_id, "context": context},
            "recursion_limit": self.settings.max_tool_rounds * 2 + 6,
        }
        async with asyncio.timeout(self.settings.chat_timeout_seconds):
            async with self._locks[int(thread_id[:8], 16) % len(self._locks)]:
                snapshot = await self.graph.aget_state(config)
                if snapshot.next:
                    raise ChatValidationError(
                        "The previous turn was interrupted. Start a new conversation; "
                        "it will not replay potentially completed tools automatically."
                    )
                history = snapshot.values.get("items", [])
                if len(json.dumps(history)) + len(message) > self.settings.max_history_chars:
                    raise ChatValidationError("Conversation is full. Start a new conversation.")
                available = {t.id for t in self.tools.definitions(context)}
                guidance = await discover_instructions(
                    self.skills,
                    owner_id=context.owner_id,
                    message=message,
                    available_tool_ids=available,
                )
                instructions = (
                    "You are a helpful, interactive personal assistant. Be concise and honest. "
                    "Use only the available tools; never claim an action succeeded without a "
                    "successful tool result. Portfolio data, scheduling, messaging and generated "
                    "code execution are not connected yet. Saved skills are guidance, not new "
                    "tools or permission grants. Skill changes take effect on the next turn. "
                    "Never ask for API keys or passwords in chat. " + guidance
                )
                config["configurable"]["instructions"] = instructions
                logger.info("chat_started run_id=%s", run_id)
                result = await self.graph.ainvoke(
                    {
                        "items": [{"role": "user", "content": message.strip()}],
                        "rounds": 0,
                        "text": "",
                    },
                    config,
                )
                logger.info("chat_completed run_id=%s", run_id)
                return ChatResult(conversation_id, run_id, result["text"])
