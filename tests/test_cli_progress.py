"""CLI displays runtime progress while preserving a single final answer."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from personal_agent.agent.progress import ChatEvent
from personal_agent.entrypoints import cli


def test_cli_prints_progress_and_final_once(monkeypatch, capsys):
    class Runtime:
        async def chat(self, *, message, conversation_id, on_event):
            assert message == "Research a company."
            assert conversation_id == "conversation"
            for sequence, (kind, text) in enumerate(
                [
                    ("started", None),
                    ("acknowledgment", "I’m working on your request."),
                    ("status", "I’m searching public sources."),
                    ("result", "Completed report."),
                ],
                start=1,
            ):
                on_event(ChatEvent(kind, "run", "conversation", sequence, text=text))
            return SimpleNamespace(conversation_id="conversation", text="Completed report.")

    @asynccontextmanager
    async def open_runtime():
        yield Runtime()

    monkeypatch.setattr(cli, "open_runtime", open_runtime)
    result = asyncio.run(
        cli.run_cli(argparse.Namespace(message="Research a company.", conversation="conversation"))
    )
    assert result == 0
    assert capsys.readouterr().out.splitlines() == [
        "Agent: I’m working on your request.",
        "Agent: I’m searching public sources.",
        "Agent: Completed report.",
        "[conversation: conversation]",
    ]


def test_cli_new_uses_runtime_reset_without_changing_conversation_identity(monkeypatch):
    calls = []

    class Runtime:
        async def chat(self, *, message, conversation_id, on_event):
            calls.append((message, conversation_id))
            return SimpleNamespace(conversation_id=conversation_id, text="Context cleared.")

    @asynccontextmanager
    async def open_runtime():
        yield Runtime()

    inputs = iter(("/new", "/exit"))
    monkeypatch.setattr(cli, "open_runtime", open_runtime)
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    result = asyncio.run(cli.run_cli(argparse.Namespace(message=None, conversation="main")))
    assert result == 0 and calls == [("/new", "main")]
