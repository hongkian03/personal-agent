"""Memory survives process restarts, retries, and forgetting without replay duplication."""

import asyncio
import json
from uuid import uuid4

from sqlalchemy import create_engine

from personal_agent.memory.service import MemoryService
from personal_agent.storage.repositories.memory import MemoryOutboxRepository


class Client:
    def __init__(self):
        self.writes = []
        self.deletes = []
        self.results = []
        self.fail = False

    async def remember(self, **kwargs):
        self.writes.append(kwargs)
        if self.fail:
            raise TimeoutError("private provider response")

    async def recall(self, **kwargs):
        if self.fail:
            raise TimeoutError("private provider response")
        return self.results

    async def forget(self, **kwargs):
        self.deletes.append(kwargs)
        if self.fail:
            raise TimeoutError("private provider response")


def repository(path):
    return MemoryOutboxRepository(create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False},
    ))


def test_failed_ingestion_survives_restart_with_same_remote_identity(tmp_path):
    async def scenario():
        path = tmp_path / "memory.sqlite"
        repo = repository(path)
        await repo.initialize()
        client = Client()
        client.fail = True
        service = MemoryService(client, repo, retry_seconds=0)
        kwargs = dict(
            owner_id="owner", conversation_id="chat", turn_id="turn",
            user_text="I work as a designer", assistant_text="You might enjoy architecture",
        )
        memory_id = await service.enqueue_exchange(**kwargs)
        assert await service.process_once()
        assert (await repo.get(owner_id="owner", memory_id=memory_id)).attempts == 1
        repo.engine.dispose()

        restarted_repo = repository(path)
        await restarted_repo.initialize()
        client.fail = False
        restarted = MemoryService(client, restarted_repo)
        assert await restarted.enqueue_exchange(**kwargs) == memory_id
        assert await restarted.process_once()
        assert not await restarted.process_once()
        assert [write["memory_id"] for write in client.writes] == [memory_id, memory_id]
        assert "User:\nI work as a designer" in client.writes[-1]["text"]
        assert "Assistant:\nYou might enjoy architecture" in client.writes[-1]["text"]
        assert client.writes[-1]["metadata"]["captured_at"]
        item = await restarted_repo.get(owner_id="owner", memory_id=memory_id)
        assert item.status == "completed"
        assert item.text == ""  # the completed outbox does not become a second transcript archive
        restarted_repo.engine.dispose()

    asyncio.run(scenario())


def test_forget_cancels_pending_capture_and_survives_replay_and_restart(tmp_path):
    async def scenario():
        path = tmp_path / "memory.sqlite"
        repo = repository(path)
        await repo.initialize()
        client = Client()
        service = MemoryService(client, repo, retry_seconds=0)
        memory_id = str(uuid4())
        await service.enqueue_memory(owner_id="owner", text="old preference", memory_id=memory_id)
        client.results = [{"memory_id": memory_id, "text": "old preference", "metadata": {}}]
        client.fail = True
        forgotten = await service.forget(owner_id="owner", memory_id=memory_id)
        assert forgotten["status"] == "delete_pending"
        assert not client.deletes  # deletion is asynchronous, not an interactive HTTP request
        assert await service.process_once()
        client.fail = False
        assert await service.recall(owner_id="owner", query="preferences") == []
        repo.engine.dispose()

        restarted_repo = repository(path)
        await restarted_repo.initialize()
        restarted = MemoryService(client, restarted_repo)
        result = await restarted.enqueue_memory(
            owner_id="owner", text="old preference", memory_id=memory_id,
        )
        assert result["status"] == "delete_pending"
        assert await restarted.process_once()
        assert not await restarted.process_once()
        assert not client.writes
        assert len(client.deletes) == 2
        item = await restarted_repo.get(owner_id="owner", memory_id=memory_id)
        assert item.status == "forgotten" and item.text == "" and item.source == {}
        result = await restarted.enqueue_memory(
            owner_id="owner", text="old preference", memory_id=memory_id,
        )
        assert result["status"] == "forgotten"
        restarted_repo.engine.dispose()

    asyncio.run(scenario())


def test_owner_isolation_bounded_context_and_fail_open(tmp_path, caplog):
    async def scenario():
        repo = repository(tmp_path / "memory.sqlite")
        await repo.initialize()
        client = Client()
        service = MemoryService(client, repo, max_context_chars=120)
        memory_id = str(uuid4())
        await service.forget(owner_id="alice", memory_id=memory_id)
        client.results = [
            {"memory_id": memory_id, "text": "x" * 300, "metadata": {}},
            {"memory_id": str(uuid4()), "text": "y" * 300, "metadata": {}},
        ]
        results = await service.recall(owner_id="bob", query="context")
        assert len(results) == 1 and set(results[0]["text"]) == {"x"}
        assert len(json.dumps(results, ensure_ascii=False)) <= 120
        results = await service.recall(owner_id="alice", query="context")
        assert len(results) == 1 and set(results[0]["text"]) == {"y"}
        client.fail = True
        assert await service.recall_context(owner_id="alice", query="context") == ""
        assert "private provider response" not in caplog.text
        repo.engine.dispose()

    asyncio.run(scenario())


def test_enqueue_and_forget_do_not_wait_for_inflight_remote_ingestion(tmp_path):
    async def scenario():
        repo = repository(tmp_path / "memory.sqlite")
        await repo.initialize()
        started, release = asyncio.Event(), asyncio.Event()

        class SlowClient(Client):
            async def remember(self, **kwargs):
                started.set()
                await release.wait()
                self.writes.append(kwargs)

        client = SlowClient()
        service = MemoryService(client, repo)
        queued = await service.enqueue_memory(owner_id="owner", text="old preference")
        processing = asyncio.create_task(service.process_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        # A stuck provider must not block a completed chat's capture or local forgetting.
        await asyncio.wait_for(service.enqueue_memory(owner_id="owner", text="new fact"), 1)
        forgotten = await asyncio.wait_for(service.forget(
            owner_id="owner", memory_id=queued["memory_id"],
        ), 1)
        assert forgotten["status"] == "delete_pending"
        release.set()
        await processing
        item = await repo.get(owner_id="owner", memory_id=queued["memory_id"])
        assert item.status == "delete_pending"  # old write's finish cannot undo deletion
        while await service.process_once():
            pass
        item = await repo.get(owner_id="owner", memory_id=queued["memory_id"])
        assert item.status == "forgotten"
        assert client.deletes == [{"owner_id": "owner", "memory_id": queued["memory_id"]}]
        repo.engine.dispose()

    asyncio.run(scenario())


def test_worker_shutdown_leaves_interrupted_ingestion_retryable(tmp_path):
    async def scenario():
        repo = repository(tmp_path / "memory.sqlite")
        await repo.initialize()
        started = asyncio.Event()

        class SlowClient(Client):
            async def remember(self, **kwargs):
                started.set()
                await asyncio.Event().wait()

        service = MemoryService(SlowClient(), repo)
        queued = await service.enqueue_memory(owner_id="owner", text="Keep this")
        await service.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        await service.close()
        item = await repo.get(owner_id="owner", memory_id=queued["memory_id"])
        assert item.status == "pending"
        client = Client()
        restarted = MemoryService(client, repo)
        assert await restarted.process_once()
        assert client.writes[0]["memory_id"] == queued["memory_id"]
        repo.engine.dispose()

    asyncio.run(scenario())
