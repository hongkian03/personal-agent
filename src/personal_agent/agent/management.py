"""Reserved conversational tool names; no handlers are implemented yet.

Management handlers will validate definitions and resolve the trusted owner from the
execution context. Tool IDs here are declarations, not executable registrations.
"""

SKILL_TOOL_IDS = (
    "skills.list",
    "skills.save_revision",
    "skills.activate",
    "skills.archive",
)
WORKFLOW_TOOL_IDS = (
    "workflows.save_revision",
    "workflows.run",
    "schedules.save",
    "schedules.disable",
)
