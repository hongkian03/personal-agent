"""Service boundary checks without downloading models or installing Cognee in the agent."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx

spec = importlib.util.spec_from_file_location(
    "cognee_service_app", Path(__file__).parents[1] / "services/cognee/app.py"
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
TOKEN = "t" * 40


class Backend:
    def __init__(self):
        self.writes = []
        self.deletes = []
        self.failure = False

    async def remember(self, record):
        self.writes.append(record)
        if self.failure:
            raise RuntimeError("provider unavailable")

    async def recall(self, record):
        return [
            SimpleNamespace(
                text="source context",
                metadata={"data_id": str(service.data_identifier(record.owner_id, "source"))},
            )
        ]

    async def forget(self, record):
        self.deletes.append(record)
        if self.failure:
            raise RuntimeError("provider unavailable")

    async def graph_snapshot(self, record):
        return "<!doctype html><html>Graph for " + record.owner_id + "</html>"


def test_graph_snapshot_auth_empty_owner_and_dataset_scope(tmp_path):
    async def check():
        app = service.create_app(
            backend=Backend(), token=TOKEN, storage_path=tmp_path / "control.sqlite",
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://memory",
            ) as client:
                assert (await client.post("/graph", json={"owner_id": "alice"})).status_code == 401
                client.headers["Authorization"] = "Bearer " + TOKEN
                empty = await client.post("/graph", json={"owner_id": "alice"})
                assert "No indexed memories" in empty.text
                await client.post("/remember", json={
                    "owner_id": "alice", "memory_id": "a", "text": "a fact",
                })
                graph = await client.post("/graph", json={"owner_id": "alice", "full": True})
                assert "Graph for alice" in graph.text
                assert graph.headers["cache-control"] == "no-store"
                assert "No indexed memories" in (await client.post(
                    "/graph", json={"owner_id": "bob"},
                )).text
    asyncio.run(check())


def test_capture_retry_recall_and_forget_survive_restart(tmp_path):
    async def scenario():
        backend = Backend()
        storage = tmp_path / "control.sqlite"
        payload = {
            "owner_id": "owner",
            "memory_id": "source",
            "text": "hello",
            "metadata": {"kind": "chat"},
        }
        for attempt in range(2):
            app = service.create_app(backend=backend, token=TOKEN, storage_path=storage)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://memory",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                ) as client:
                    assert (await client.post("/remember", json=payload)).status_code == 200
                    assert len(backend.writes) == 1
                    changed = await client.post("/remember", json={**payload, "text": "different"})
                    assert changed.status_code == 409
                    result = await client.post(
                        "/recall", json={"owner_id": "owner", "query": "hello"}
                    )
                    assert result.json()["results"][0]["memory_id"] == "source"
                    assert result.json()["results"][0]["metadata"] == {"kind": "chat"}
                    other = await client.post(
                        "/recall", json={"owner_id": "another", "query": "hello"}
                    )
                    assert other.json() == {"results": []}
                    if attempt:
                        forgotten = await client.post(
                            "/forget", json={"owner_id": "owner", "memory_id": "source"}
                        )
                        assert forgotten.status_code == 200
                        late = await client.post("/remember", json=payload)
                        assert late.json()["status"] == "forgotten"
                        empty = await client.post(
                            "/recall", json={"owner_id": "owner", "query": "hello"}
                        )
                        assert empty.json() == {"results": []}
        assert len(backend.deletes) == 1

    asyncio.run(scenario())


def test_failure_retries_and_forget_before_ingestion(tmp_path, caplog):
    async def scenario():
        backend = Backend()
        app = service.create_app(
            backend=backend, token=TOKEN, storage_path=tmp_path / "control.sqlite"
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://memory",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client:
                payload = {"owner_id": "owner", "memory_id": "source", "text": "hello"}
                backend.failure = True
                assert (await client.post("/remember", json=payload)).status_code == 503
                backend.failure = False
                assert (await client.post("/remember", json=payload)).status_code == 200
                assert len(backend.writes) == 2
                await client.post(
                    "/forget", json={"owner_id": "owner", "memory_id": "not-yet-written"}
                )
                late = await client.post(
                    "/remember", json={**payload, "memory_id": "not-yet-written"}
                )
                assert late.json()["status"] == "forgotten"
                assert len(backend.writes) == 2
                backend.failure = True
                assert (
                    await client.post("/forget", json={"owner_id": "owner", "memory_id": "source"})
                ).status_code == 503
                assert (
                    await client.post("/recall", json={"owner_id": "owner", "query": "hello"})
                ).json() == {"results": []}
                backend.failure = False
                assert (
                    await client.post("/forget", json={"owner_id": "owner", "memory_id": "source"})
                ).status_code == 200

    asyncio.run(scenario())
    assert "error_type=RuntimeError" in caplog.text
    assert "provider unavailable" not in caplog.text


def test_requires_auth_and_owner_scope(tmp_path):
    async def scenario():
        backend = Backend()
        app = service.create_app(
            backend=backend, token=TOKEN, storage_path=tmp_path / "control.sqlite"
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://memory"
            ) as client:
                assert (await client.get("/health")).status_code == 200
                assert (
                    await client.post("/recall", json={"owner_id": "owner", "query": "x"})
                ).status_code == 401
                assert not backend.writes

    asyncio.run(scenario())


def test_sdk_calls_use_stable_source_ids_and_retrieval_only(monkeypatch):
    async def scenario():
        calls = {}

        async def remember(item, **kwargs):
            calls["remember"] = (item, kwargs)
            return SimpleNamespace(status="completed")

        async def recall(query, **kwargs):
            calls["recall"] = (query, kwargs)
            return []

        async def forget(**kwargs):
            calls["forget"] = kwargs

        monkeypatch.setitem(
            sys.modules, "cognee", SimpleNamespace(remember=remember, recall=recall, forget=forget)
        )
        monkeypatch.setitem(
            sys.modules,
            "cognee.tasks.ingestion.data_item",
            SimpleNamespace(DataItem=lambda **kwargs: SimpleNamespace(**kwargs)),
        )
        monkeypatch.setitem(
            sys.modules,
            "cognee.api.v1.search",
            SimpleNamespace(SearchType=SimpleNamespace(CHUNKS="chunks")),
        )
        backend = service.CogneeBackend()
        record = service.Remember(
            owner_id="owner",
            memory_id="source",
            text="https://example.com",
            metadata={"source": "chat"},
        )
        await backend.remember(record)
        item, kwargs = calls["remember"]
        assert isinstance(item.data_id, UUID)
        assert item.data.startswith("Personal agent memory record:\n")
        assert kwargs["run_in_background"] is False
        assert kwargs["dataset_name"] == service.dataset_name("owner")
        await backend.recall(service.Recall(owner_id="owner", query="what?", limit=3))
        assert calls["recall"][1]["query_type"] == "chunks"
        assert calls["recall"][1]["top_k"] == 3
        await backend.forget(service.MemoryIdentity(owner_id="owner", memory_id="source"))
        assert calls["forget"]["data_id"] == item.data_id
        assert calls["forget"]["dataset"] == kwargs["dataset_name"]

    asyncio.run(scenario())


def test_recall_does_not_wait_for_indexing_and_rechecks_tombstones(tmp_path):
    async def scenario():
        class ConcurrentBackend(Backend):
            def __init__(self):
                super().__init__()
                self.indexing = asyncio.Event()
                self.finish_indexing = asyncio.Event()
                self.reading = asyncio.Event()
                self.finish_reading = asyncio.Event()
                self.pause_read = False

            async def remember(self, record):
                if record.memory_id == "second":
                    self.indexing.set()
                    await self.finish_indexing.wait()
                await super().remember(record)

            async def recall(self, record):
                if self.pause_read:
                    self.reading.set()
                    await self.finish_reading.wait()
                return await super().recall(record)

        backend = ConcurrentBackend()
        app = service.create_app(
            backend=backend, token=TOKEN, storage_path=tmp_path / "control.sqlite"
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://memory",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client:
                payload = {"owner_id": "owner", "memory_id": "source", "text": "hello"}
                await client.post("/remember", json=payload)
                write = asyncio.create_task(
                    client.post("/remember", json={**payload, "memory_id": "second"})
                )
                await backend.indexing.wait()
                result = await asyncio.wait_for(
                    client.post("/recall", json={"owner_id": "owner", "query": "hello"}), timeout=1
                )
                assert result.json()["results"][0]["memory_id"] == "source"
                backend.finish_indexing.set()
                await write
                backend.pause_read = True
                read = asyncio.create_task(
                    client.post("/recall", json={"owner_id": "owner", "query": "hello"})
                )
                await backend.reading.wait()
                await client.post("/forget", json={"owner_id": "owner", "memory_id": "source"})
                backend.finish_reading.set()
                assert (await read).json() == {"results": []}

    asyncio.run(scenario())


def test_sdk_logging_defaults_are_applied_before_lazy_import(tmp_path, monkeypatch):
    for name in ("LOG_LEVEL", "TELEMETRY_DISABLED", "COGNEE_TRACING_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    app = service.create_app(
        backend=Backend(), token=TOKEN, storage_path=tmp_path / "control.sqlite"
    )
    import os

    assert os.environ["LOG_LEVEL"] == "WARNING"
    assert os.environ["TELEMETRY_DISABLED"] == "true"
    assert os.environ["COGNEE_TRACING_ENABLED"] == "false"

    async def close():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(close())
