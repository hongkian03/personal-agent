"""Shared, editable persona instructions for model-generated messages."""

from pathlib import Path

MAX_PERSONA_CHARS = 12_000


def with_persona(instructions: str, path: Path) -> str:
    """Read on each generation so local edits apply without restarting the process.

    A missing file preserves the generic assistant behavior for installations that
    do not ship a profile. Task instructions remain after the profile so a persona
    does not replace output requirements or tool/authorization boundaries.
    """
    try:
        with path.open(encoding="utf-8") as profile:
            persona = profile.read(MAX_PERSONA_CHARS + 1)
    except FileNotFoundError:
        return instructions
    if len(persona) > MAX_PERSONA_CHARS:
        raise ValueError("Agent persona must be at most 12000 characters")
    if not persona.strip():
        return instructions
    return (
        "Shared agent persona and personal context:\n"
        + persona.strip()
        + "\n\nTask instructions and runtime boundaries:\n"
        + instructions
        + "\n\nUse the current shared profile above for your name, persona, and communication "
        "preferences, even if older conversation messages or saved skills describe a "
        "different identity or style. Task requirements and tool permissions still apply."
    )
