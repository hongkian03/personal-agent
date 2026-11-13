"""HTTP boundary to the separately hosted Cognee memory service."""

from personal_agent.integrations.cognee.client import CogneeClient, CogneeError

__all__ = ["CogneeClient", "CogneeError"]
