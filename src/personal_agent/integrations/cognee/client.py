"""Authenticated client for our pinned Cognee service wrapper, not the native API.

The wrapper owns upstream SDK details. Callers supply stable memory IDs so an
interrupted ingestion can safely retry without creating a second document.
"""

from typing import Any

import httpx


class CogneeError(RuntimeError):
    """A memory request failed; messages never include provider response bodies."""


class CogneeClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout_seconds: float = 10,
        ingest_timeout_seconds: float = 180,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Cognee URL must be an HTTP(S) service URL without credentials")
        if not api_token.strip() or timeout_seconds <= 0 or ingest_timeout_seconds <= 0:
            raise ValueError("Cognee requires a token and positive request deadlines")
        self.base_url = str(url).rstrip("/")
        self._token = api_token
        self.timeout_seconds = timeout_seconds
        self.ingest_timeout_seconds = ingest_timeout_seconds
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, path: str, payload: dict, *, ingestion: bool = False) -> dict:
        try:
            response = await self._client.post(
                self.base_url + path,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=self.ingest_timeout_seconds if ingestion else self.timeout_seconds,
                follow_redirects=False,
            )
            if response.status_code >= 300:
                raise CogneeError(f"Memory service request failed (HTTP {response.status_code})")
            if len(response.content) > 1_000_000:
                raise CogneeError("Memory service returned an oversized response")
            result = response.json()
            if not isinstance(result, dict):
                raise CogneeError("Memory service returned an invalid response")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise CogneeError(
                "Memory service is unavailable or returned an invalid response"
            ) from exc

    async def remember(
        self, *, owner_id: str, memory_id: str, text: str, metadata: dict[str, Any]
    ) -> None:
        result = await self._post(
            "/remember",
            {"owner_id": owner_id, "memory_id": memory_id, "text": text, "metadata": metadata},
            ingestion=True,
        )
        if (
            result.get("status") not in {"remembered", "forgotten"}
            or result.get("memory_id") != memory_id
        ):
            raise CogneeError("Memory service did not confirm ingestion")

    async def recall(self, *, owner_id: str, query: str, limit: int = 5) -> list[dict]:
        result = await self._post(
            "/recall", {"owner_id": owner_id, "query": query, "limit": max(1, min(limit, 20))}
        )
        entries = result.get("results")
        if not isinstance(entries, list):
            raise CogneeError("Memory service returned invalid retrieval results")
        memories = []
        remaining = 20_000
        for entry in entries[:max(1, min(limit, 20))]:
            if not isinstance(entry, dict):
                raise CogneeError("Memory service returned an invalid memory")
            memory_id, text = entry.get("memory_id"), entry.get("text")
            metadata = entry.get("metadata", {})
            if (
                not isinstance(memory_id, str) or not memory_id or len(memory_id) > 128
                or not isinstance(text, str) or not isinstance(metadata, dict)
            ):
                raise CogneeError("Memory service returned an invalid memory")
            if not text.strip() or remaining <= 0:
                continue
            bounded_text = text[:min(remaining, 8_000)]
            # Only small provenance fields cross into chat, never arbitrary nested objects.
            bounded_metadata = {
                key: value[:500] for key, value in metadata.items()
                if key in {
                    "source", "conversation_id", "turn_id", "created_at", "captured_at", "kind",
                    "role",
                }
                and isinstance(value, str)
            }
            memories.append({
                "memory_id": memory_id, "text": bounded_text, "metadata": bounded_metadata,
            })
            remaining -= len(bounded_text)
        return memories

    async def forget(self, *, owner_id: str, memory_id: str) -> None:
        result = await self._post(
            "/forget", {"owner_id": owner_id, "memory_id": memory_id}, ingestion=True
        )
        if result.get("status") != "forgotten" or result.get("memory_id") != memory_id:
            raise CogneeError("Memory service did not confirm deletion")
