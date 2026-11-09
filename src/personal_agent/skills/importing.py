"""Package editable skill files and apply changed packages at runtime startup."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.skills.models import SkillRevision
from personal_agent.skills.service import SkillService


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    skill_id: str
    name: str
    description: str
    required_tool_ids: list[str]


class PackagedSkill(SkillMetadata):
    instructions: str
    activate: bool = False

    def revision(self, owner_id: str) -> SkillRevision:
        return SkillRevision(
            **self.model_dump(exclude={"activate", "required_tool_ids"}),
            required_tool_ids=tuple(self.required_tool_ids),
            owner_id=owner_id,
            revision=1,  # The repository assigns the actual next revision atomically.
            created_at=datetime.now(timezone.utc),
        )

    def digest(self) -> str:
        payload = self.model_dump()
        payload["required_tool_ids"] = sorted(self.required_tool_ids)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class SkillBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: int = 1
    skills: list[PackagedSkill] = Field(default_factory=list, max_length=100)


def read_bundle(path: Path) -> SkillBundle:
    bundle = SkillBundle.model_validate_json(path.read_text(encoding="utf-8"))
    if bundle.version != 1:
        raise ValueError("Unsupported skill bundle version")
    ids = [skill.skill_id for skill in bundle.skills]
    if len(ids) != len(set(ids)):
        raise ValueError("Skill bundle contains duplicate skill IDs")
    return bundle


def validate_bundle(bundle: SkillBundle, service: SkillService, owner_id: str) -> None:
    for skill in bundle.skills:
        service.validate(skill.revision(owner_id))


def prepare_import(
    directory: Path,
    bundle_path: Path,
    *,
    activate: bool,
    available_tool_ids: set[str],
) -> PackagedSkill:
    """Validate and atomically update a deployable bundle; never open a database."""
    metadata = SkillMetadata.model_validate_json(
        (directory / "metadata.json").read_text(encoding="utf-8")
    )
    skill = PackagedSkill(
        **metadata.model_dump(),
        instructions=(directory / "instructions.md").read_text(encoding="utf-8"),
        activate=activate,
    )
    bundle = read_bundle(bundle_path) if bundle_path.exists() else SkillBundle()
    entries = {entry.skill_id: entry for entry in bundle.skills}
    entries[skill.skill_id] = skill
    bundle = SkillBundle(skills=[entries[key] for key in sorted(entries)])
    # Validation needs no repository access.
    validate_bundle(bundle, SkillService(None, available_tool_ids), "package-validation")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=bundle_path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(bundle.model_dump_json(indent=2) + "\n")
        os.replace(temporary, bundle_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return skill


async def apply_bundle(
    path: Path, *, service: SkillService, owner_id: str
) -> list[tuple[str, int, bool]]:
    if not path.exists():
        return []
    bundle = read_bundle(path)
    # Reject invalid entries before importing any of this bundle's skills.
    validate_bundle(bundle, service, owner_id)
    results = []
    for skill in bundle.skills:
        revision, changed = await service.repository.import_revision(
            skill.revision(owner_id), source_digest=skill.digest(), activate=skill.activate
        )
        results.append((skill.skill_id, revision, changed))
    return results
