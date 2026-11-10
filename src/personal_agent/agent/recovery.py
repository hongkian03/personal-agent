"""Close unfinished turns without deleting completed conversation context."""

import asyncio
import hashlib
import json
from typing import Any

import anyio

from personal_agent.core.context import ExecutionContext


def original_request(state: dict[str, Any]) -> str:
    """Older checkpoints predate the explicit request field."""
    request = state.get("request")
    if isinstance(request, str) and request.strip():
        return request
    for item in reversed(state.get("items", [])):
        if item.get("role") == "user" and isinstance(item.get("content"), str):
            return item["content"]
    return "Continue the previous request using the available conversation context."


def turn_identifier(state: dict[str, Any]) -> str:
    turn_id = state.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    legacy = [
        original_request(state),
        [call.get("call_id") for call in state.get("pending_calls", [])],
    ]
    return "legacy-" + hashlib.sha256(json.dumps(legacy).encode()).hexdigest()


def is_recovery_command(message: str) -> bool:
    text = message.strip()
    return text == "/recovery" or text == "/resolve" or text.startswith("/resolve ")


async def preserve_hosted_usage(graph, config, exc: BaseException, max_calls: int) -> None:
    """Account for failed searches without persisting partial provider output."""
    used_calls = getattr(exc, "hosted_tool_calls", 0)
    if not isinstance(used_calls, int) or used_calls <= 0:
        return
    # Cleanup stays under the conversation lock, including disconnect cancellation.
    # A bounded shield allows the journal write without extending work indefinitely.
    with anyio.CancelScope(shield=True):
        async with asyncio.timeout(2):
            failed = await graph.aget_state(config)
            if tuple(failed.next) == ("model",):
                count = failed.values.get("hosted_tool_calls", 0) + used_calls
                await graph.aupdate_state(
                    config, {"hosted_tool_calls": min(count, max_calls)}, as_node="resume",
                )


async def close_interrupted_turn(graph, tools, snapshot, config, context: ExecutionContext) -> None:
    """Supply results for orphaned tool calls and close execution at a terminal node."""
    state = snapshot.values
    completed_ids = {
        item.get("call_id") for item in state.get("items", [])
        if item.get("type") == "function_call_output"
    }
    pending = [call for call in state.get("pending_calls", [])
               if call.get("call_id") not in completed_ids]
    outputs: list[dict[str, Any]] = []
    if pending:
        recover = getattr(tools, "recover_outputs", None)
        if recover is not None:
            outputs = await recover(
                context=context, turn_id=turn_identifier(state), calls=pending,
            )
        else:
            # Compatibility for old checkpoints/custom registries: no pending call is
            # executed while abandoning a turn, and no successful action is invented.
            outputs = [
                {
                    "type": "function_call_output", "call_id": call["call_id"],
                    "output": json.dumps({
                        "ok": False,
                        "error": "The previous turn was interrupted. This action was not replayed; "
                                 "its outcome may be unknown.",
                    }),
                }
                for call in pending
            ]
    outputs.append({
        "role": "assistant",
        "content": "The previous request stopped before a final answer. Completed tool results "
                   "are retained. Unfinished actions were not replayed; "
                   "do not assume they succeeded.",
    })
    await graph.aupdate_state(
        config, {"items": outputs, "pending_calls": [], "text": ""}, as_node="recovery",
    )
