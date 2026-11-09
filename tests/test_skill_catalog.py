"""Skill discovery and loading through the real tool loop, without model API calls."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent import bootstrap
from personal_agent.agent.models import ModelReply, ToolCall
from personal_agent.agent.tools import ToolError
from personal_agent.config import Settings
from personal_agent.core.context import ExecutionContext
from personal_agent.skills.discovery import discover_skills, exact_matches
from personal_agent.skills.models import SkillRevision


def skill(skill_id="company-research", **overrides):
    values = dict(
        skill_id=skill_id,
        owner_id="alice",
        revision=1,
        name="Company research",
        description="Explain businesses, competitors, recent developments and risks.",
        instructions="INSTRUCTIONS: Separate established facts from uncertainty.",
        required_tool_ids=(),
        created_at=datetime.now(timezone.utc),
    )
    values.update(overrides)
    return SkillRevision(**values)


def discover(revisions, message="How does Nvidia make money?", **limits):
    repository = SimpleNamespace(list_active=AsyncMock(return_value=tuple(revisions)))
    guidance = asyncio.run(
        discover_skills(
            repository,
            owner_id="alice",
            message=message,
            available_tool_ids={"skills.list", "skills.load"},
            **limits,
        )
    )
    return json.loads(guidance.split("\n", 1)[1])


def test_catalog_includes_skills_without_shared_keywords_and_does_not_load_bodies():
    revisions = [skill(f"research-{i}", name=f"Company research {i}") for i in range(8)]
    payload = discover(reversed(revisions))
    assert [entry["skill_id"] for entry in payload["catalog"]] == [
        revision.skill_id for revision in revisions
    ]
    assert all(set(entry) == {"skill_id", "name", "description"} for entry in payload["catalog"])
    assert payload["loaded_skills"] == []
    assert "INSTRUCTIONS:" not in json.dumps(payload)


@pytest.mark.parametrize(
    "message, matches",
    [
        ("Use Company research on Nvidia", True),
        ("Use **COMPANY   RESEARCH** on Nvidia", True),
        ("Use company\nresearch on Nvidia", True),
        ("Use $company-research", True),
        ('Load "company-research".', True),
        ("Use company-researcher", False),
        ("Use non-company-research", False),
        ("Use mycompany-research", False),
        ("Research this company", False),
    ],
)
def test_exact_selection_preserves_whole_names_and_ids(message, matches):
    loaded = discover([skill()], message)["loaded_skills"]
    assert len(loaded) == int(matches)
    if matches:
        assert loaded[0]["instructions"] == skill().instructions
        assert loaded[0]["revision"] == 1


def test_ambiguous_names_do_not_load_both_and_longest_name_wins():
    first = skill("first")
    second = skill("second")
    assert exact_matches("Use company research", (first, second)) == ()
    assert exact_matches("Use first", (first, second)) == (first,)
    shorter = skill("short", name="Research")
    assert exact_matches("Use company research", (first, second, shorter)) == ()
    assert exact_matches("Use company research", (first, shorter)) == (first,)
    assert exact_matches("Use company research, then research", (first, shorter)) == (
        first,
        shorter,
    )
    # Identifiers are authoritative even when another skill has that display name.
    collision = skill("third", name="first")
    assert exact_matches("Use first", (first, collision)) == (first,)


def test_catalog_and_exact_selection_exclude_foreign_or_unavailable_skills():
    payload = discover(
        [
            skill(),
            skill("foreign", owner_id="bob", name="Foreign secret"),
            skill("blocked", name="Blocked", required_tool_ids=("portfolio.get_summary",)),
        ],
        "Use foreign and blocked and company-research",
    )
    assert [entry["skill_id"] for entry in payload["catalog"]] == ["company-research"]
    assert [entry["skill_id"] for entry in payload["loaded_skills"]] == ["company-research"]
    assert "Foreign secret" not in json.dumps(payload)


def test_budget_preserves_exact_reference_and_falls_back_to_list_and_load():
    revisions = [skill(f"skill-{i}", name=f"Named skill {i}") for i in range(8)]
    payload = discover(revisions, "Use skill-7", max_catalog_chars=250, max_loaded_chars=20)
    assert payload["catalog"][0]["skill_id"] == "skill-7"
    assert len(json.dumps(payload["catalog"], ensure_ascii=False)) <= 250
    assert payload["catalog_omitted"] == len(revisions) - len(payload["catalog"])
    assert payload["loaded_skills"] == []
    assert payload["exact_matches_to_load"] == ["skill-7"]
    empty = discover([], max_catalog_chars=0, max_loaded_chars=0)
    assert empty == {"catalog": [], "loaded_skills": []}


def settings_for(tmp_path):
    return Settings(
        _env_file=None,
        model_api_key="not-a-real-key",
        database_url=None,
        data_dir=tmp_path,
        skill_bundle_path=tmp_path / "no-bundle.json",
        owner_id="alice",
        ibkr_flex_token=None,
        ibkr_flex_query_id=None,
        web_search_enabled=False,
    )


def context(owner="alice", scopes=("skills:read", "skills:write")):
    return ExecutionContext(owner_id=owner, run_id="test", granted_scopes=frozenset(scopes))


async def save(repository, revision, *, active=True):
    await repository.save_revision(
        revision,
        expected_latest_revision=None if revision.revision == 1 else revision.revision - 1,
    )
    if active:
        await repository.activate(
            owner_id=revision.owner_id, skill_id=revision.skill_id, revision=revision.revision
        )


def test_load_uses_current_active_revision_and_never_changes_activation(tmp_path):
    async def check():
        async with bootstrap.open_runtime(settings_for(tmp_path)) as runtime:
            await save(runtime.skills, skill())
            await save(runtime.skills, skill(revision=2, instructions="New draft"), active=False)
            await save(runtime.skills, skill("foreign", owner_id="bob"))
            await save(runtime.skills, skill("draft"), active=False)
            await save(runtime.skills, skill("archived"))
            await runtime.skills.archive(owner_id="alice", skill_id="archived")
            await save(runtime.skills, skill("blocked", required_tool_ids=("missing.tool",)))
            await save(runtime.skills, skill("scoped", required_tool_ids=("skills.save_revision",)))
            tools = runtime.tools
            before = await runtime.skills.list_active(owner_id="alice")
            loaded = await tools.invoke(
                tool_id="skills.load",
                arguments={"skill_id": "company-research"},
                context=context(scopes=("skills:read",)),
            )
            assert loaded["revision"] == 1 and loaded["instructions"] == skill().instructions
            assert await runtime.skills.list_active(owner_id="alice") == before
            assert not next(
                t for t in tools.definitions(context()) if t.id == "skills.load"
            ).changes_state
            for skill_id in ("foreign", "draft", "archived", "blocked", "scoped", "unknown"):
                with pytest.raises(ToolError, match="not active or is unavailable"):
                    await tools.invoke(
                        tool_id="skills.load",
                        arguments={"skill_id": skill_id},
                        context=context(scopes=("skills:read",)),
                    )
            with pytest.raises(ToolError, match="not authorized"):
                await tools.invoke(
                    tool_id="skills.load",
                    arguments={"skill_id": "company-research"},
                    context=context(scopes=()),
                )
            with pytest.raises(ToolError, match="schema"):
                await tools.invoke(
                    tool_id="skills.load",
                    arguments={"skill_id": "foreign", "owner_id": "bob"},
                    context=context(),
                )
            catalog = await tools.invoke(
                tool_id="skills.list", arguments={}, context=context(scopes=("skills:read",))
            )
            assert [s["skill_id"] for s in catalog["skills"]] == ["company-research"]
            assert "instructions" not in catalog["skills"][0]
            await runtime.skills.activate(owner_id="alice", skill_id="company-research", revision=2)
            updated = await tools.invoke(
                tool_id="skills.load", arguments={"skill_id": "company-research"}, context=context()
            )
            assert updated["revision"] == 2 and updated["instructions"] == "New draft"

    asyncio.run(check())


def test_save_activate_list_load_and_archive_remain_available_as_chat_tools(tmp_path):
    async def check():
        async with bootstrap.open_runtime(settings_for(tmp_path)) as runtime:

            async def invoke(tool_id, **arguments):
                return await runtime.tools.invoke(
                    tool_id=tool_id, arguments=arguments, context=context()
                )

            saved = await invoke(
                "skills.save_revision",
                skill_id="company-research",
                name=skill().name,
                description=skill().description,
                instructions=skill().instructions,
                required_tool_ids=[],
                expected_latest_revision=None,
            )
            assert saved["revision"] == 1 and saved["active"] is False
            assert (await invoke("skills.list"))["skills"] == []
            with pytest.raises(ToolError, match="not active"):
                await invoke("skills.load", skill_id="company-research")
            await invoke("skills.activate", skill_id="company-research", revision=1)
            assert (await invoke("skills.list"))["skills"][0]["name"] == skill().name
            loaded = await invoke("skills.load", skill_id="company-research")
            assert loaded["instructions"] == skill().instructions
            await invoke("skills.archive", skill_id="company-research")
            assert (await invoke("skills.list"))["skills"] == []
            with pytest.raises(ToolError, match="not active"):
                await invoke("skills.load", skill_id="company-research")

    asyncio.run(check())


class CatalogModel:
    """Select from metadata, then require real tool output before answering."""

    def __init__(self, **kwargs):
        self.calls = []

    async def respond(self, *, items, instructions, tools, **kwargs):
        self.calls.append((list(items), instructions))
        assert "skills.load" in {tool.id for tool in tools}
        if items[-1].get("type") == "function_call_output":
            result = json.loads(items[-1]["output"])
            assert result["ok"]
            assert result["result"]["instructions"] == skill().instructions
            return ModelReply(
                items=[{"role": "assistant", "content": "Applied the loaded skill."}],
                text="Applied the loaded skill.",
                tool_calls=[],
            )
        # Exact references are already loaded before the first model call.
        if skill().instructions in instructions:
            return ModelReply(
                items=[{"role": "assistant", "content": "Used the named skill."}],
                text="Used the named skill.",
                tool_calls=[],
            )
        assert '"skill_id": "company-research"' in instructions
        arguments = json.dumps({"skill_id": "company-research"})
        return ModelReply(
            items=[
                {
                    "type": "function_call",
                    "call_id": f"load-{len(self.calls)}",
                    "name": "skills__load",
                    "arguments": arguments,
                }
            ],
            text="",
            tool_calls=[ToolCall(f"load-{len(self.calls)}", "skills__load", arguments)],
        )

    async def aclose(self):
        pass


def test_catalog_load_and_explicit_selection_are_wired_through_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "OpenAIModel", CatalogModel)

    async def check():
        async with bootstrap.open_runtime(settings_for(tmp_path)) as runtime:
            await save(runtime.skills, skill())
            model = runtime.morning.model
            reply = await runtime.chat("How does Nvidia make money?", "catalog-test")
            assert reply.text == "Applied the loaded skill."
            assert len(model.calls) == 2
            assert skill().instructions not in model.calls[0][1]
            # Follow-ups can select using existing conversation context without keywords.
            await runtime.chat("Do the same for AMD.", "catalog-test")
            assert any(
                item.get("content") == "How does Nvidia make money?" for item in model.calls[2][0]
            )
            assert skill().instructions not in model.calls[2][1]
            reply = await runtime.chat("Use COMPANY RESEARCH on Intel", "explicit-test")
            assert reply.text == "Used the named skill."
            assert len(model.calls) == 5  # No load/tool round for the direct reference.
            assert skill().instructions in model.calls[-1][1]
            # Exact matches use the active revision, not a newer inactive draft.
            await save(
                runtime.skills, replace(skill(), revision=2, instructions="Draft"), active=False
            )
            reply = await runtime.chat("Use $company-research", "explicit-test")
            assert reply.text == "Used the named skill."
            assert '"revision": 1' in model.calls[-1][1]

    asyncio.run(check())
