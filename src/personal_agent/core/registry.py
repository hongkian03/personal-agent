"""Explicit capability discovery; importing a capability performs no I/O."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityDefinition:
    id: str
    description: str
    workflow_ids: tuple[str, ...]
    tool_ids: tuple[str, ...] = ()


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDefinition] = {}

    def register(self, capability: CapabilityDefinition) -> None:
        if capability.id in self._capabilities:
            raise ValueError(f"Duplicate capability: {capability.id}")
        existing = {
            identifier
            for item in self._capabilities.values()
            for identifier in (*item.workflow_ids, *item.tool_ids)
        }
        identifiers = (*capability.workflow_ids, *capability.tool_ids)
        if len(set(identifiers)) != len(identifiers) or existing.intersection(identifiers):
            raise ValueError(f"Duplicate workflow or tool identifier in {capability.id}")
        self._capabilities[capability.id] = capability

    def list(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(self._capabilities.values())
