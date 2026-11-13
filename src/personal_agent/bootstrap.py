"""Composition root: build providers, tools, persistence and the interactive runtime."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from personal_agent.agent.graph import build_graph
from personal_agent.agent.management import SKILL_TOOL_IDS
from personal_agent.agent.progress_messages import ProgressMessages
from personal_agent.agent.service import AgentRuntime
from personal_agent.agent.tools import ToolDefinition, ToolError, ToolRegistry
from personal_agent.capabilities.motivation.manifest import CAPABILITY as MOTIVATION
from personal_agent.capabilities.motivation.workflows import MorningMessage
from personal_agent.capabilities.portfolio.briefing import PortfolioMorningMessage
from personal_agent.capabilities.portfolio.manifest import CAPABILITY as PORTFOLIO
from personal_agent.capabilities.portfolio.tools import register_portfolio_tools
from personal_agent.capabilities.portfolio.workflows import PortfolioService
from personal_agent.capabilities.web.manifest import CAPABILITY as WEB
from personal_agent.config import Settings
from personal_agent.core.context import ExecutionContext
from personal_agent.core.registry import CapabilityRegistry
from personal_agent.integrations.cognee import CogneeClient
from personal_agent.integrations.cognee.tools import register_memory_tools
from personal_agent.integrations.ibkr.client import IBKRFlexClient
from personal_agent.integrations.llm.provider import OpenAIModel
from personal_agent.memory.service import MemoryService
from personal_agent.skills.discovery import available_skills, skill_details, skill_summary
from personal_agent.skills.importing import apply_bundle
from personal_agent.skills.service import SkillService
from personal_agent.storage.database import open_storage


def build_registry(*, web_search_enabled: bool = True) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for capability in (PORTFOLIO, MOTIVATION):
        registry.register(capability)
    if web_search_enabled:
        registry.register(WEB)
    return registry


def skill_tool_ids(*, portfolio_enabled: bool = True, memory_enabled: bool = False) -> set[str]:
    """Application tools supported by skill imports and conversational authoring."""
    return set(SKILL_TOOL_IDS) | {"runtime.current_time"} | (
        set(PORTFOLIO.tool_ids) if portfolio_enabled else set()
    ) | ({"memory.search", "memory.remember", "memory.forget"} if memory_enabled else set())


def build_tools(
    *, repository, settings: Settings, portfolio=None, operations=None, memory=None,
) -> ToolRegistry:
    tools = ToolRegistry(timeout_seconds=settings.tool_timeout_seconds, operations=operations)
    service = SkillService(
        repository,
        available_tool_ids=skill_tool_ids(
            portfolio_enabled=portfolio is not None, memory_enabled=memory is not None,
        ),
    )

    def register(
        name, description, properties, required, scope, handler,
        *, mutates=False, reconcile=None,
    ):
        tools.register(
            ToolDefinition(
                id=name,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                required_scopes=frozenset({scope}),
                changes_state=mutates,
            ),
            handler,
            reconcile=reconcile,
        )

    async def current_time(arguments: dict, context: ExecutionContext) -> dict:
        return {
            "time": datetime.now(ZoneInfo(settings.timezone)).isoformat(),
            "timezone": settings.timezone,
        }

    async def list_skills(arguments: dict, context: ExecutionContext) -> dict:
        revisions = available_skills(
            await repository.list_active(owner_id=context.owner_id),
            owner_id=context.owner_id,
            available_tool_ids={tool.id for tool in tools.definitions(context)},
        )
        return {
            "skills": [
                {**skill_summary(s), "revision": s.revision}
                for s in revisions
            ]
        }

    async def load_skill(arguments: dict, context: ExecutionContext) -> dict:
        try:
            skill = await service.load(
                owner_id=context.owner_id,
                skill_id=arguments["skill_id"],
                available_tool_ids={tool.id for tool in tools.definitions(context)},
            )
        except LookupError as exc:
            raise ToolError(str(exc)) from exc
        return {
            **skill_details(skill),
            "note": "Read-only guidance for this task, not a permission grant or new tool. "
            "Loading does not save, activate or execute a skill.",
        }

    async def save_skill(arguments: dict, context: ExecutionContext) -> dict:
        skill = await service.save_revision(
            owner_id=context.owner_id,
            **{**arguments, "required_tool_ids": tuple(arguments["required_tool_ids"])},
        )
        return {
            "skill_id": skill.skill_id,
            "revision": skill.revision,
            "active": False,
            "note": "Activate this revision to use it on future turns.",
        }

    async def activate_skill(arguments: dict, context: ExecutionContext) -> dict:
        await service.activate(owner_id=context.owner_id, **arguments)
        return {**arguments, "active": True}

    async def archive_skill(arguments: dict, context: ExecutionContext) -> dict:
        await service.archive(owner_id=context.owner_id, **arguments)
        return {**arguments, "active": False}

    async def reconcile_save(arguments: dict, context: ExecutionContext) -> dict | None:
        expected = arguments["expected_latest_revision"]
        target = 1 if expected is None else expected + 1
        try:
            saved = await repository.get_revision(
                owner_id=context.owner_id, skill_id=arguments["skill_id"], revision=target,
            )
        except LookupError:
            return None
        fields = ("name", "description", "instructions")
        if any(getattr(saved, field) != arguments[field] for field in fields):
            return None
        if tuple(saved.required_tool_ids) != tuple(arguments["required_tool_ids"]):
            return None
        return {
            "skill_id": saved.skill_id, "revision": saved.revision, "active": False,
            "note": "The matching saved revision confirms this action completed.",
        }

    async def reconcile_activation(arguments: dict, context: ExecutionContext) -> dict | None:
        try:
            active = await repository.get_activation(
                owner_id=context.owner_id, skill_id=arguments["skill_id"],
            )
        except (LookupError, AttributeError):
            return None
        target = arguments.get("revision")
        if active != target:
            return None
        return {**arguments, "active": target is not None, "reconciled": True}

    skill_id = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,119}$"}
    register(
        "runtime.current_time",
        "Get the current time in the configured timezone.",
        {},
        [],
        "runtime:read",
        current_time,
    )
    register(
        "skills.list",
        "List the full catalog of active reusable skills available for this execution. "
        "Returns names, descriptions, IDs and revisions; use skills.load for instructions.",
        {},
        [],
        "skills:read",
        list_skills,
    )
    register(
        "skills.load",
        "Read full instructions for a selected active skill by its exact skill_id. "
        "Select using the catalog and conversation context; load before use unless the current "
        "turn already supplies its instructions. "
        "This does not change activation or execute the skill.",
        {"skill_id": skill_id},
        ["skill_id"],
        "skills:read",
        load_skill,
    )
    register(
        "skills.save_revision",
        "Save reusable instructions, initially inactive. "
        "Use null expected_latest_revision for a new skill, or its latest revision to edit.",
        {
            "skill_id": skill_id,
            "name": {"type": "string", "maxLength": 200},
            "description": {"type": "string", "maxLength": 1000},
            "instructions": {"type": "string", "maxLength": 12000},
            "required_tool_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
                "maxItems": 50,
            },
            "expected_latest_revision": {"type": ["integer", "null"], "minimum": 1},
        },
        [
            "skill_id",
            "name",
            "description",
            "instructions",
            "required_tool_ids",
            "expected_latest_revision",
        ],
        "skills:write",
        save_skill,
        mutates=True,
        reconcile=reconcile_save,
    )
    register(
        "skills.activate",
        "Activate a saved skill revision, or roll back to an older one.",
        {"skill_id": skill_id, "revision": {"type": "integer", "minimum": 1}},
        ["skill_id", "revision"],
        "skills:write",
        activate_skill,
        mutates=True,
        reconcile=reconcile_activation,
    )
    register(
        "skills.archive",
        "Remove a skill from discovery while preserving its revisions.",
        {"skill_id": skill_id},
        ["skill_id"],
        "skills:write",
        archive_skill,
        mutates=True,
        reconcile=reconcile_activation,
    )
    if portfolio is not None:
        register_portfolio_tools(tools, portfolio)
    if memory is not None:
        register_memory_tools(tools, memory)
    return tools


async def run_portfolio_sync(service, interval_seconds: float) -> None:
    """Check periodically; the service coalesces refreshes and backs off failed attempts."""
    while True:
        try:
            await service.sync()
        except Exception as exc:
            # Never log provider exceptions: they may contain broker URLs or report data.
            logging.getLogger(__name__).warning(
                "portfolio_sync_failed error_type=%s", type(exc).__name__
            )
        await asyncio.sleep(min(60, interval_seconds))


@asynccontextmanager
async def open_runtime(settings: Settings | None = None, *, ibkr_client=None):
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    database_url = settings.database_url.get_secret_value() if settings.database_url else None
    async with open_storage(database_url=database_url, data_dir=settings.data_dir) as storage:
        client = ibkr_client
        if client is None and settings.ibkr_flex_token and settings.ibkr_flex_query_id:
            client = IBKRFlexClient(
                token=settings.ibkr_flex_token.get_secret_value(),
                query_id=settings.ibkr_flex_query_id,
                account_ids=settings.ibkr_accounts,
            )
        model = None
        progress_model = None
        sync_task = None
        memory = None
        memory_client = None
        try:
            if settings.memory_enabled:
                memory_client = CogneeClient(
                    base_url=settings.cognee_url,
                    api_token=settings.cognee_api_token.get_secret_value(),
                    timeout_seconds=settings.memory_recall_timeout_seconds,
                    ingest_timeout_seconds=settings.memory_ingest_timeout_seconds,
                )
                memory = MemoryService(
                    memory_client, storage.memory,
                    poll_interval_seconds=settings.memory_poll_interval_seconds,
                    retry_seconds=settings.memory_retry_seconds,
                    max_context_chars=settings.memory_max_context_chars,
                )
                await memory.start()
            portfolio = PortfolioService(
                storage.portfolio,
                client,
                settings.owner_id,
                timezone=settings.timezone,
                sync_interval_seconds=settings.portfolio_sync_interval_seconds,
            )
            tools = build_tools(
                repository=storage.skills, settings=settings, portfolio=portfolio,
                operations=storage.tool_operations,
                memory=memory,
            )
            imported = await apply_bundle(
                settings.skill_bundle_path,
                service=SkillService(
                    storage.skills, skill_tool_ids(memory_enabled=memory is not None),
                ),
                owner_id=settings.owner_id,
            )
            for skill_id, revision, changed in imported:
                if changed:
                    logging.getLogger(__name__).info(
                        "skill_imported skill=%s revision=%s", skill_id, revision
                    )
            graph = None
            if settings.model_api_key:
                model = OpenAIModel(
                    api_key=settings.model_api_key.get_secret_value(),
                    model=settings.model,
                    timeout_seconds=settings.model_timeout_seconds,
                    max_output_tokens=settings.max_output_tokens,
                    web_search_context_size=settings.web_search_context_size,
                    max_attempts=settings.model_max_attempts,
                    retry_delay_seconds=settings.model_retry_delay_seconds,
                )
                graph = build_graph(
                    model=model,
                    tools=tools,
                    checkpointer=storage.checkpointer,
                    max_tool_rounds=settings.max_tool_rounds,
                    tool_timeout_seconds=settings.tool_timeout_seconds,
                    web_search_max_calls=settings.web_search_max_calls,
                )
                if settings.progress_enabled and settings.progress_dynamic_enabled:
                    progress_model = OpenAIModel(
                        api_key=settings.model_api_key.get_secret_value(), model=settings.model,
                        timeout_seconds=3, max_output_tokens=256, max_attempts=1,
                    )
            morning = (
                MorningMessage(
                    model,
                    storage.morning,
                    settings.owner_id,
                    ZoneInfo(settings.timezone),
                    persona_path=settings.persona_path,
                    memory=memory,
                    memory_timeout_seconds=settings.memory_recall_timeout_seconds,
                )
                if model
                else None
            )
            portfolio_morning = (
                PortfolioMorningMessage(
                    portfolio,
                    storage.morning,
                    settings.owner_id,
                    ZoneInfo(settings.timezone),
                    focused_symbols=settings.focused_symbols,
                )
                if settings.portfolio_morning_enabled
                else None
            )
            if client is not None:
                sync_task = asyncio.create_task(
                    run_portfolio_sync(portfolio, settings.portfolio_sync_interval_seconds),
                    name="ibkr-portfolio-sync",
                )
            yield AgentRuntime(
                settings=settings,
                graph=graph,
                tools=tools,
                skills=storage.skills,
                checkpointer=storage.checkpointer,
                morning=morning,
                portfolio=portfolio,
                portfolio_morning=portfolio_morning,
                memory=memory,
                progress_messages=(
                    ProgressMessages(progress_model, settings.persona_path)
                    if progress_model else None
                ),
            )
        finally:
            if progress_model is not None:
                await progress_model.aclose()
            if memory is not None:
                await memory.close()
            if memory_client is not None:
                await memory_client.aclose()
            if sync_task is not None:
                sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sync_task
            try:
                if client is not None:
                    await client.aclose()
            finally:
                if model:
                    await model.aclose()
