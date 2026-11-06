"""Hosted search metadata; the model adapter executes its built-in tool."""

from personal_agent.core.registry import CapabilityDefinition

CAPABILITY = CapabilityDefinition(
    id="web",
    description="Search current public information with source links using hosted web search.",
    workflow_ids=(),
)
