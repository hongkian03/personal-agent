# Develop skills in files

## Edit, package, deploy

Create a directory under `skill_templates/` containing `metadata.json` and
`instructions.md`. The [stock research template](stock-research.md) is an example.
Metadata must contain exactly these fields:

```json
{
  "skill_id": "my-skill",
  "name": "My skill",
  "description": "When the agent should select this skill.",
  "required_tool_ids": ["runtime.current_time"]
}
```

Write the procedure in `instructions.md`, then run from the repository root:

```sh
uv run personal-agent skills import skill_templates/my-skill --activate
```

The command validates both files and updates `config/skills.json`, merging by skill ID
and keeping other packaged skills. It prints the destination and does not open a database,
call a model, or contact Railway. Omit `--activate` to prepare an inactive revision;
this leaves an already active revision in place.

Commit the source directory **and `config/skills.json`**, then push/deploy normally.
The Docker build includes the bundle. On startup, `agent-api` validates it, saves changed
skills into its own database under its configured owner, and activates entries marked
for activation. No payload copying, remote shell command, or bridge deployment is needed.
The next chat turn can discover the skill. Importing does not execute it or schedule it.

For local use, start/restart the CLI or API after packaging; a running process does not
watch the bundle. Local and Railway revision numbers are independent.

Interactive skills inherit the runtime's [conversation progress policy](progress.md).
Do not add acknowledgment timers or status-message tools to individual skills. Global
progress settings control the behavior for every conversation, including skill-free tasks.

## Updates and repeated deployments

After changing either source file, rerun the same command and commit the regenerated
bundle. Editing source files without rerunning the command does not change the package.

Each database tracks the last applied package fingerprint per owner and skill. Identical
packages are skipped on later startups, so redeployments do not create duplicate revisions
or undo subsequent conversational edits, activation changes, rollbacks, or archives.
A changed package appends a revision after the latest saved revision and, with
`--activate`, makes it active. This deliberately supersedes any intervening chat edits.
Changing the activation flag also counts as a package change.

Rerunning an unchanged package does not reactivate an archived skill. Use the existing
`skills.activate` chat tool for that. Removing an entry from the bundle does not delete
or archive database records; use `skills.archive` to hide an installed skill.

Validation checks IDs, text limits, metadata types, duplicate required tools, and known
application tool IDs. Invalid input leaves the previous bundle intact. Startup validates
all entries before writing any of them; each individual skill's revision, activation,
and import fingerprint are committed in one transaction. A failure later in a multi-skill
import may leave earlier entries applied; retrying startup skips those completed entries.
An invalid bundle stops startup rather than silently running a different skill version.

## Files and methods

- `src/personal_agent/entrypoints/cli.py`: parses `skills import` and calls `prepare_import`.
- `src/personal_agent/skills/importing.py`: reads and validates source files, writes the
  bundle atomically, and implements startup application with `apply_bundle`.
- `src/personal_agent/bootstrap.py`: calls `apply_bundle` before accepting chat requests.
- `src/personal_agent/storage/repositories/skills.py`: `import_revision` assigns revisions,
  persists activation and import fingerprints, and detects concurrent revision changes.
- `config/skills.json`: generated deployment input. Review it in Git; fine-tune the
  original template files and regenerate it rather than editing this file directly.

By default startup reads `config/skills.json`; an absent bundle is allowed. Advanced
setups can use `--bundle PATH` when packaging and matching
`PERSONAL_AGENT_SKILL_BUNDLE_PATH` in the runtime. Custom locations also need to be
included in that deployment's Docker build.
