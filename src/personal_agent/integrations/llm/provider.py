"""OpenAI Responses API adapter; the graph owns conversation persistence."""

from typing import Any

from openai import AsyncOpenAI, OpenAIError

from personal_agent.agent.models import ModelError, ModelReply, ToolCall
from personal_agent.agent.tools import ToolDefinition


class OpenAIModel:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 60,
        max_output_tokens: int = 2048,
    ) -> None:
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=2)

    async def aclose(self) -> None:
        await self.client.close()

    async def respond(
        self,
        *,
        items: list[dict[str, Any]],
        instructions: str,
        tools: tuple[ToolDefinition, ...],
    ) -> ModelReply:
        functions = [
            {
                "type": "function",
                "name": definition.model_name,
                "description": definition.description,
                "parameters": definition.input_schema,
                "strict": False,
            }
            for definition in tools
        ]
        try:
            response = await self.client.responses.create(
                model=self.model,
                instructions=instructions,
                input=items,
                tools=functions,
                max_output_tokens=self.max_output_tokens,
                store=False,
                include=["reasoning.encrypted_content"],
                parallel_tool_calls=False,
            )
        except OpenAIError as exc:
            # Provider exceptions can contain request bodies; don't expose them to clients.
            raise ModelError(
                "OpenAI request failed; check credentials, model and connectivity"
            ) from exc
        if response.status != "completed":
            raise ModelError(
                "OpenAI did not complete the response; retry or increase output limits"
            )
        # Preserve reasoning and function-call items for subsequent requests, as required
        # by the Responses API. Never reconstruct history from output_text alone.
        output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        calls = [
            ToolCall(call_id=item.call_id, name=item.name, arguments=item.arguments)
            for item in response.output
            if item.type == "function_call"
        ]
        if not output:
            raise ModelError("OpenAI returned an empty response")
        text = response.output_text
        if not text.strip() and not calls:
            text = "\n".join(
                part.refusal
                for item in response.output
                if item.type == "message"
                for part in item.content
                if part.type == "refusal"
            )
            if not text.strip():
                raise ModelError("OpenAI returned no visible reply or tool calls")
        return ModelReply(items=output, text=text, tool_calls=calls)
