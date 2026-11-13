"""Single-process HTTP boundary around Cognee's pinned Python SDK.

The agent owns delivery retries. This service acknowledges writes only after indexing
and keeps stable source identifiers and deletion tombstones across restarts.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)


class MemoryIdentity(Identity):
    memory_id: str = Field(min_length=1, max_length=256)


class Remember(MemoryIdentity):
    text: str = Field(min_length=1, max_length=100000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Recall(Identity):
    query: str = Field(min_length=1, max_length=12000)
    limit: int = Field(default=5, ge=1, le=20)


class GraphSnapshot(Identity):
    full: bool = False


def dataset_name(owner_id: str) -> str:
    return "personal_" + hashlib.sha256(owner_id.encode()).hexdigest()


def data_identifier(owner_id: str, memory_id: str):
    return uuid5(NAMESPACE_URL, json.dumps([owner_id, memory_id]))


class CogneeBackend:
    """Lazy imports keep the agent and wrapper contract tests independent of the SDK."""

    async def remember(self, record: Remember):
        import cognee
        from cognee.tasks.ingestion.data_item import DataItem

        # Prefix ensures user text is never interpreted as a file path or URL.
        item = DataItem(
            data="Personal agent memory record:\n"
            + json.dumps(
                {
                    "memory_id": record.memory_id,
                    "text": record.text,
                    "metadata": record.metadata,
                },
                ensure_ascii=False,
            ),
            data_id=data_identifier(record.owner_id, record.memory_id),
            external_metadata={**record.metadata, "memory_id": record.memory_id},
        )
        result = await cognee.remember(
            item,
            dataset_name=dataset_name(record.owner_id),
            run_in_background=False,
            self_improvement=True,
        )
        if getattr(result, "status", None) != "completed":
            raise RuntimeError("Cognee indexing did not complete")

    async def recall(self, record: Recall):
        import cognee
        from cognee.api.v1.search import SearchType

        # Return source chunks rather than a second model's generated answer.
        return await cognee.recall(
            record.query,
            datasets=[dataset_name(record.owner_id)],
            query_type=SearchType.CHUNKS,
            top_k=record.limit,
        )

    async def forget(self, record: MemoryIdentity):
        import cognee

        await cognee.forget(
            dataset=dataset_name(record.owner_id),
            data_id=data_identifier(record.owner_id, record.memory_id),
        )

    async def graph_snapshot(self, record: GraphSnapshot) -> str:
        import cognee

        with tempfile.TemporaryDirectory(prefix="cognee-graph-") as directory:
            destination = Path(directory) / "memory-graph.html"
            await cognee.visualize_graph(
                str(destination), dataset=dataset_name(record.owner_id),
                full=record.full, max_nodes=500, include_session_events=False,
            )
            return await asyncio.to_thread(destination.read_text, encoding="utf-8")


def create_app(*, backend=None, token: str | None = None, storage_path: Path | None = None):
    # Cognee reads these on first SDK import; INFO retrieval logs include queries.
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("TELEMETRY_DISABLED", "true")
    os.environ.setdefault("COGNEE_TRACING_ENABLED", "false")
    token = token if token is not None else os.getenv("COGNEE_SERVICE_TOKEN", "")
    if len(token) < 32:
        raise ValueError("COGNEE_SERVICE_TOKEN must contain at least 32 characters")
    backend = backend or CogneeBackend()
    path = storage_path or Path(
        os.getenv("COGNEE_CONTROL_DATABASE", "/cognee-storage/control.sqlite")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS memories (
        owner_id TEXT NOT NULL, memory_id TEXT NOT NULL, data_id TEXT NOT NULL,
        digest TEXT NOT NULL, status TEXT NOT NULL, metadata TEXT NOT NULL,
        PRIMARY KEY(owner_id, memory_id))""")
    db.commit()
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials.encode(), token.encode())
        ):
            raise HTTPException(status_code=401, detail="Authentication required")

    def lookup(record):
        return db.execute(
            "SELECT * FROM memories WHERE owner_id=? AND memory_id=?",
            (record.owner_id, record.memory_id),
        ).fetchone()

    def save(record, *, digest: str, status: str, metadata: dict):
        db.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,memory_id) "
            "DO UPDATE SET digest=excluded.digest,status=excluded.status,"
            "metadata=excluded.metadata",
            (
                record.owner_id,
                record.memory_id,
                str(data_identifier(record.owner_id, record.memory_id)),
                digest,
                status,
                json.dumps(metadata),
            ),
        )
        db.commit()

    def missing(error):
        return type(error).__name__ in {
            "DatasetNotFoundError",
            "DataNotFoundError",
            "DatabaseNotCreatedError",
            "UserNotFoundError",
        }

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/graph", dependencies=[Depends(authenticate)], response_class=HTMLResponse)
    async def graph_snapshot(record: GraphSnapshot):
        # Export in the owning process, serialized against indexing and deletion.
        async with lock:
            exists = db.execute(
                "SELECT 1 FROM memories WHERE owner_id=? AND status='remembered' LIMIT 1",
                (record.owner_id,),
            ).fetchone()
            if not exists:
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>Memory graph</title></head><body><h1>No indexed memories</h1>"
                    "<p>This owner has no completed memory sources to visualize.</p></body></html>"
                )
            else:
                try:
                    html = await backend.graph_snapshot(record)
                except Exception as error:
                    logger.warning("memory_graph_failed error_type=%s", type(error).__name__)
                    raise HTTPException(
                        status_code=503, detail="Graph export unavailable"
                    ) from None
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.post("/remember", dependencies=[Depends(authenticate)])
    async def remember(record: Remember):
        metadata_json = json.dumps(record.metadata, sort_keys=True, allow_nan=False)
        if len(metadata_json) > 16000:
            raise HTTPException(status_code=422, detail="Memory metadata is too large")
        digest = hashlib.sha256((record.text + metadata_json).encode()).hexdigest()
        async with lock:
            previous = lookup(record)
            if previous and previous["status"] == "forgotten":
                # A delayed retry must not resurrect a deleted source.
                return {"status": "forgotten", "memory_id": record.memory_id}
            if previous and previous["digest"] != digest:
                raise HTTPException(
                    status_code=409, detail="Memory ID already has different content"
                )
            if not previous or previous["status"] != "remembered":
                save(record, digest=digest, status="pending", metadata=record.metadata)
                try:
                    await backend.remember(record)
                except Exception as error:
                    logger.warning("memory_indexing_failed error_type=%s", type(error).__name__)
                    raise HTTPException(
                        status_code=503, detail="Memory indexing unavailable"
                    ) from None
                save(record, digest=digest, status="remembered", metadata=record.metadata)
        return {"status": "remembered", "memory_id": record.memory_id}

    @app.post("/recall", dependencies=[Depends(authenticate)])
    async def recall(record: Recall):
        # Cognee permits recall during indexing. Do not queue interactive reads behind
        # model-heavy writes. Re-check each receipt after retrieval so a concurrent
        # tombstone can never be returned from an earlier read snapshot.
        exists = db.execute(
            "SELECT 1 FROM memories WHERE owner_id=? AND status='remembered' LIMIT 1",
            (record.owner_id,),
        ).fetchone()
        if not exists:
            return {"results": []}
        try:
            retrieved = await backend.recall(record)
        except Exception as error:
            if missing(error):
                return {"results": []}
            logger.warning("memory_retrieval_failed error_type=%s", type(error).__name__)
            raise HTTPException(status_code=503, detail="Memory retrieval unavailable") from None
        results = []
        for item in retrieved:
            meta = (
                item.get("metadata", {})
                if isinstance(item, dict)
                else getattr(item, "metadata", {})
            )
            text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
            data_id = str(meta.get("data_id", "")) if isinstance(meta, dict) else ""
            row = db.execute(
                "SELECT * FROM memories WHERE owner_id=? AND data_id=? AND status='remembered'",
                (record.owner_id, data_id),
            ).fetchone()
            if row and isinstance(text, str) and text.strip():
                results.append(
                    {
                        "memory_id": row["memory_id"],
                        "text": text[:12000],
                        "metadata": json.loads(row["metadata"]),
                    }
                )
            if len(results) >= record.limit:
                break
        return {"results": results}

    @app.post("/forget", dependencies=[Depends(authenticate)])
    async def forget(record: MemoryIdentity):
        async with lock:
            previous = lookup(record)
            save(
                record,
                digest=previous["digest"] if previous else "",
                status="forgotten",
                metadata={},
            )
            if previous:
                try:
                    await backend.forget(record)
                except Exception as error:
                    if not missing(error):
                        logger.warning("memory_deletion_failed error_type=%s", type(error).__name__)
                        raise HTTPException(
                            status_code=503, detail="Memory deletion unavailable"
                        ) from None
        return {"status": "forgotten", "memory_id": record.memory_id}

    return app


if __name__ == "__main__":
    import socket

    import uvicorn

    # Explicit dual-stack socket accepts Railway private IPv6 and local IPv4 probes.
    listener = socket.create_server(
        ("::", int(os.getenv("PORT", "8000"))),
        family=socket.AF_INET6,
        dualstack_ipv6=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app,
            factory=True,
            access_log=False,
            workers=1,
        )
    )
    server.run(sockets=[listener])
