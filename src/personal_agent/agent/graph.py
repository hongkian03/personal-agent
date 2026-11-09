"""Bounded conversational model/tool loop with injected checkpoint storage."""

import asyncio
import json
import logging
import operator
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from personal_agent.agent.models import ActivityEvent, ChatModel
from personal_agent.agent.progress import report_activity
from personal_agent.agent.tools import ToolError, ToolRegistry
from personal_agent.core.context import ExecutionContext

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    items: Annotated[list[dict[str, Any]], operator.add]
    pending_calls: list[dict[str, str]]
    text: str
    rounds: int
    hosted_tool_calls: int


def build_graph(
    *,
    model: ChatModel,
    tools: ToolRegistry,
    checkpointer: Any,
    max_tool_rounds: int = 8,
    tool_timeout_seconds: float = 30,
    web_search_max_calls: int = 3,
):
    """Compile the graph; context/instructions are supplied via configurable per turn."""
    if max_tool_rounds < 1 or tool_timeout_seconds <= 0 or web_search_max_calls < 1:
        raise ValueError("Execution limits must be positive")

    def context_of(config: RunnableConfig) -> ExecutionContext:
        context = config.get("configurable", {}).get("context")
        if not isinstance(context, ExecutionContext):
            raise ValueError("Trusted ExecutionContext is required")
        return context

    async def call_model(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        context = context_of(config)
        instructions = config.get("configurable", {}).get("instructions", "")
        used_calls = state.get("hosted_tool_calls", 0)
        remaining_calls = max(0, web_search_max_calls - used_calls)
        hosted_tools = (
            frozenset({"web_search"})
            if "web:search" in context.granted_scopes and remaining_calls
            else frozenset()
        )
        if "web:search" in context.granted_scopes and not remaining_calls:
            instructions += (
                "\nThe web search budget for this turn has been reached. "
                "Use the sources already retrieved, state any remaining uncertainty, "
                "and do not claim to have performed additional searches."
            )
        report_activity(ActivityEvent("model_working"))
        reply = await model.respond(
            items=state.get("items", []),
            instructions=instructions,
            tools=tools.definitions(context),
            hosted_tools=hosted_tools,
            max_hosted_tool_calls=max(1, remaining_calls),
            on_activity=report_activity,
        )
        return {
            "items": reply.items,
            "text": reply.text,
            "hosted_tool_calls": used_calls + reply.hosted_tool_calls,
            "pending_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in reply.tool_calls
            ],
        }

    async def call_tools(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        context = context_of(config)
        outputs = []
        for index, call in enumerate(state.get("pending_calls", [])):
            try:
                if index >= 8:
                    raise ToolError("Maximum tool calls per round exceeded")
                arguments = json.loads(call["arguments"])
                if not isinstance(arguments, dict):
                    raise ToolError("Tool arguments must be an object")
                async with asyncio.timeout(tool_timeout_seconds):
                    tool_id = tools.resolve_name(call["name"])
                    report_activity(ActivityEvent("tool_started", tool_id=tool_id))
                    result = await tools.invoke(
                        tool_id=tool_id,
                        arguments=arguments,
                        context=context,
                    )
                output = json.dumps({"ok": True, "result": result}, allow_nan=False)
            except (ToolError, json.JSONDecodeError) as exc:
                output = json.dumps({"ok": False, "error": str(exc)[:300]})
            except TimeoutError:
                output = json.dumps({"ok": False, "error": "Tool execution timed out"})
            except Exception:
                logger.warning("tool_execution_failed run=%s", context.run_id)
                output = json.dumps({"ok": False, "error": "Tool execution failed"})
            finally:
                report_activity(ActivityEvent("tool_completed"))
            outputs.append(
                {"type": "function_call_output", "call_id": call["call_id"], "output": output}
            )
        return {"items": outputs, "pending_calls": [], "rounds": state.get("rounds", 0) + 1}

    async def limit_reached(state: AgentState) -> dict[str, Any]:
        text = "I reached the tool execution limit for this turn. Please narrow the request."
        return {"text": text, "items": [{"role": "assistant", "content": text}]}

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", call_tools)
    graph.add_node("limit", limit_reached)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model", lambda state: "tools" if state.get("pending_calls") else END
    )
    graph.add_conditional_edges(
        "tools", lambda state: "limit" if state["rounds"] >= max_tool_rounds else "model"
    )
    graph.add_edge("limit", END)
    return graph.compile(checkpointer=checkpointer)
