"""Conversational tool IDs; skill handlers are bound in bootstrap.

Workflow and schedule IDs remain declarations without executable handlers.
"""

SKILL_TOOL_IDS = (
    "skills.list",
    "skills.load",
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
