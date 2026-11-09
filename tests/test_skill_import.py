"""File-to-deployment skill imports, restart behavior and CLI compatibility."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event, select

from personal_agent.bootstrap import open_runtime
from personal_agent.config import Settings
from personal_agent.entrypoints import cli
from personal_agent.skills.importing import apply_bundle, prepare_import, read_bundle
from personal_agent.skills.service import SkillService
from personal_agent.storage.repositories.skills import SqlSkillRepository, heads, imports

TOOLS = {"runtime.current_time"}


def template(root, skill_id="research", instructions="Check dated sources."):
    directory = root / skill_id
    directory.mkdir(exist_ok=True)
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "skill_id": skill_id,
                "name": "Company research",
                "description": "Research a public company.",
                "required_tool_ids": ["runtime.current_time"],
            }
        )
    )
    (directory / "instructions.md").write_text(instructions)
    return directory


def package(directory, bundle, activate=True):
    return prepare_import(directory, bundle, activate=activate, available_tool_ids=TOOLS)


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'skills.sqlite'}")
    repository = SqlSkillRepository(engine)
    asyncio.run(repository.initialize())
    yield repository
    engine.dispose()


def test_packaging_merges_skills_and_is_repeatable_without_database(tmp_path):
    bundle = tmp_path / "config" / "skills.json"
    first = template(tmp_path, "first")
    second = template(tmp_path, "second")
    package(second, bundle)
    package(first, bundle)
    content = bundle.read_bytes()
    package(first, bundle)
    assert bundle.read_bytes() == content
    assert [skill.skill_id for skill in read_bundle(bundle).skills] == ["first", "second"]
    assert not list(tmp_path.rglob("*.sqlite"))


@pytest.mark.parametrize("invalid", ["unknown_tool", "extra_field", "wrong_type", "blank"])
def test_invalid_template_preserves_bundle(tmp_path, invalid):
    directory = template(tmp_path)
    bundle = tmp_path / "skills.json"
    package(directory, bundle)
    before = bundle.read_bytes()
    path = directory / "metadata.json"
    metadata = json.loads(path.read_text())
    if invalid == "unknown_tool":
        metadata["required_tool_ids"] = ["web.search"]
    elif invalid == "extra_field":
        metadata["owner_id"] = "someone-else"
    elif invalid == "wrong_type":
        metadata["required_tool_ids"] = "runtime.current_time"
    else:
        (directory / "instructions.md").write_text(" ")
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        package(directory, bundle)
    assert bundle.read_bytes() == before


def test_import_revisions_activation_owner_scope_and_restart(repository, tmp_path):
    directory = template(tmp_path)
    bundle = tmp_path / "skills.json"
    package(directory, bundle)
    service = SkillService(repository, TOOLS)

    async def apply(owner="alice"):
        return await apply_bundle(bundle, service=service, owner_id=owner)

    async def check():
        assert await apply() == [("research", 1, True)]
        assert (await repository.list_active(owner_id="alice"))[0].revision == 1
        assert await apply() == [("research", 1, False)]
        assert await apply("bob") == [("research", 1, True)]
        (directory / "instructions.md").write_text("Compare recent filings.")
        package(directory, bundle, activate=False)
        assert await apply() == [("research", 2, True)]
        # An inactive import leaves the previous active revision available.
        assert (await repository.list_active(owner_id="alice"))[0].revision == 1
        assert (
            await repository.get_revision(owner_id="alice", skill_id="research", revision=2)
        ).instructions == "Compare recent filings."
        package(directory, bundle, activate=True)
        assert await apply() == [("research", 3, True)]
        assert (await repository.list_active(owner_id="alice"))[0].revision == 3
        assert (await repository.list_active(owner_id="bob"))[0].revision == 1

    asyncio.run(check())


def test_unchanged_package_preserves_chat_edits_and_archives(repository, tmp_path):
    directory = template(tmp_path)
    bundle = tmp_path / "skills.json"
    package(directory, bundle)
    service = SkillService(repository, TOOLS)

    async def apply():
        return await apply_bundle(bundle, service=service, owner_id="alice")

    async def check():
        await apply()
        await service.save_revision(
            owner_id="alice",
            skill_id="research",
            name="Chat revision",
            description="Updated in chat.",
            instructions="Chat-specific instructions.",
            required_tool_ids=(),
            expected_latest_revision=1,
        )
        await service.activate(owner_id="alice", skill_id="research", revision=2)
        assert await apply() == [("research", 1, False)]
        assert (await repository.list_active(owner_id="alice"))[0].revision == 2
        await service.archive(owner_id="alice", skill_id="research")
        assert await apply() == [("research", 1, False)]
        assert await repository.list_active(owner_id="alice") == ()
        (directory / "instructions.md").write_text("New repository instructions.")
        package(directory, bundle)
        assert await apply() == [("research", 3, True)]
        assert (await repository.list_active(owner_id="alice"))[0].revision == 3

    asyncio.run(check())


def test_invalid_bundle_validates_every_entry_before_writing(repository, tmp_path):
    bundle = tmp_path / "skills.json"
    package(template(tmp_path, "first"), bundle)
    package(template(tmp_path, "second"), bundle)
    payload = json.loads(bundle.read_text())
    payload["skills"][1]["required_tool_ids"] = ["nonexistent"]
    bundle.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(apply_bundle(bundle, service=SkillService(repository, TOOLS), owner_id="alice"))
    with repository.engine.connect() as connection:
        assert connection.execute(select(heads)).first() is None


def test_import_failure_rolls_back_revision_activation_and_stamp(repository, tmp_path):
    bundle = tmp_path / "skills.json"
    package(template(tmp_path), bundle)

    def fail_stamp(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO skill_imports"):
            raise RuntimeError("Simulated storage failure")

    event.listen(repository.engine, "before_cursor_execute", fail_stamp)
    with pytest.raises(RuntimeError, match="Simulated"):
        asyncio.run(apply_bundle(bundle, service=SkillService(repository, TOOLS), owner_id="alice"))
    event.remove(repository.engine, "before_cursor_execute", fail_stamp)
    with repository.engine.connect() as connection:
        assert connection.execute(select(heads)).first() is None
        assert connection.execute(select(imports)).first() is None
    result = asyncio.run(
        apply_bundle(bundle, service=SkillService(repository, TOOLS), owner_id="alice")
    )
    assert result == [("research", 1, True)]


def test_runtime_startup_imports_without_model_and_restarts_cleanly(tmp_path):
    bundle = tmp_path / "skills.json"
    package(template(tmp_path), bundle)
    settings = Settings(
        _env_file=None,
        model_api_key=None,
        database_url=None,
        ibkr_flex_token=None,
        ibkr_flex_query_id=None,
        data_dir=tmp_path / "data",
        skill_bundle_path=bundle,
        owner_id="alice",
    )

    async def check():
        for _ in range(2):
            async with open_runtime(settings) as runtime:
                active = await runtime.skills.list_active(owner_id="alice")
                assert len(active) == 1
                assert active[0].revision == 1
                assert active[0].instructions == "Check dated sources."

    asyncio.run(check())


def test_cli_packages_and_old_chat_arguments_still_work(tmp_path, monkeypatch, capsys):
    directory = template(tmp_path)
    bundle = tmp_path / "skills.json"
    with pytest.raises(SystemExit) as result:
        cli.main(["skills", "import", str(directory), "--activate", "--bundle", str(bundle)])
    assert result.value.code == 0
    assert read_bundle(bundle).skills[0].activate is True
    assert "No database was changed" in capsys.readouterr().out
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "run_cli", runner)
    with pytest.raises(SystemExit) as result:
        cli.main(["--message", "Hello", "--conversation", "existing"])
    assert result.value.code == 0
    args = runner.call_args.args[0]
    assert args.message == "Hello" and args.conversation == "existing"


def test_cli_rejects_missing_files_without_bundle(tmp_path, capsys):
    bundle = tmp_path / "skills.json"
    with pytest.raises(SystemExit) as result:
        cli.main(["skills", "import", str(tmp_path), "--bundle", str(bundle)])
    assert result.value.code == 1
    assert "Skill import failed" in capsys.readouterr().err
    assert not bundle.exists()
