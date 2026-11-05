"""Motivation capability declaration; execution is not implemented yet."""

from personal_agent.core.registry import CapabilityDefinition

CAPABILITY = CapabilityDefinition(
    id="motivation",
    description="Generate personalized morning encouragement without repetition.",
    workflow_ids=("motivation.morning_message",),
)
