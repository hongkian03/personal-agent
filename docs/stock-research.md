# Stock research skill scaffold

This is an editable template for the application's saved-skill system. It is not
automatically loaded, saved, activated, or scheduled by the runtime.

## Files to edit

- [`metadata.json`](../skill_templates/stock-research/metadata.json): skill ID, display
  name, catalog description, and required application tool IDs.
- [`instructions.md`](../skill_templates/stock-research/instructions.md): research
  procedure, optional portfolio context, and default report outline.

Start by editing the report outline and source preferences. The description helps the
agent choose this skill from its catalog, so keep it aligned with the instructions.
The instructions must stay within the saved-skill limit of 12,000 characters.

Hosted `web_search` is supplied by the model adapter, not registered as an application
function tool. Do not add `web_search` or an invented `web.search` ID to
`required_tool_ids`: the current skill validator would reject it. The instructions
require the agent to disclose when current research cannot be verified. Search still
depends on the runtime's web-search configuration and per-turn budget.

Portfolio tools are optional here, so they are not declared as hard requirements.
See [web search](web-search.md) for current retrieval limits and [IBKR reporting](ibkr.md)
for the saved portfolio data available to the agent.

## Save after reviewing

From the repository root, generate the exact arguments for `skills.save_revision`:

```sh
uv run python - <<'PY'
import json
from pathlib import Path

template = Path("skill_templates/stock-research")
payload = json.loads((template / "metadata.json").read_text(encoding="utf-8"))
payload["instructions"] = (template / "instructions.md").read_text(encoding="utf-8")
payload["expected_latest_revision"] = None
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
```

This command only prints the payload. In a conversation with your personal agent,
ask it to save a skill revision using that payload and leave it inactive. If the ID
already exists, supply its latest saved revision as `expected_latest_revision` instead
of `null`. Saving a revision does not activate it.

Once reviewed, ask the agent to activate the returned revision of `stock-research`.
Then try: "Use Stock research on AMD." Saving and activation must happen against the
intended runtime: a local database and the Railway database are separate.

Editing or deploying these template files does not update a saved revision. To apply
later changes, generate a new payload, save a new revision with the correct expected
latest revision, and activate that revision. For the general lifecycle, see
[skills and extensions](extensions.md).
