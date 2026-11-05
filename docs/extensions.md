# Adding functionality through conversation

## Supported architectural levels

1. Preferences and schedules: edit persistent settings and timing through validated tools.
2. Skills: save reusable natural-language instructions for existing tools.
3. Workflows: save versioned, structured sequences using existing tool handlers.
4. Executable extensions: future generated scripts or installed integrations, isolated from
   the stable runtime and subject to validation and scoped authorization.

The runtime implements owner-scoped skill persistence, bounded lexical discovery, and
chat tools to list active skills, save revisions, activate/roll back, and archive them.
Saved-workflow execution, schedule management, and sandbox execution remain contracts
only. Skills that require unavailable tools are rejected or excluded from retrieval.
Tool-less skills can teach conversational procedures, tone and response structure.

## Example: weekly portfolio review

User: "Every Friday morning, review my portfolio and finish with practical encouragement."

- Agent checks available tools, asks for missing details, and identifies required scopes.
- If necessary it saves a skill describing how to review the data and write the commentary.
- Management tools validate tool availability and append immutable skill/workflow revisions.
- A schedule records the owner's timezone, recurrence, missed-run policy, and workflow revision.
- At execution, the worker loads that exact revision and required skill revisions.
- The runner checks authorization and argument schemas through the common tool invoker.
- It records step outputs and prepares an outgoing message for the transport layer.
- Delivery and conversation records retain the source run so the user can ask follow-ups.

A skill is instructions, not a permission grant or a callable Python handler. It may be
used by an interactive agent or a registered agent-invocation tool in a saved workflow.
Workflow and skill names do not automatically create executable tools.

## Definition formats and validation

SkillRevision stores owner, ID, revision, instructions, description, required tool IDs,
and creation timestamp. Retrieval loads only relevant active revisions for that owner.
Activation selects a saved revision; rollback selects a previous revision. Append uses
optimistic concurrency to prevent silently overwriting concurrent edits. Archive preserves
history but removes a skill from discovery.

WorkflowRevision initially models sequential registered-tool calls with literal JSON
arguments and pinned skill references. Workflow dataclasses are contracts, not validators; SkillService validates skills.
References between step outputs, branching, loops, runtime variables and compensating
operations are not implemented. Define and validate a structured binding format before
adding those features; never evaluate templates as Python. Any future agent step must be
an explicitly registered tool with bounded execution.

Workflow validation must reject unknown tools, invalid schemas, unauthorized calls,
foreign-owner skill references, missing revisions, duplicate step IDs, and oversized
plans. Credentials and arbitrary executable source must not be stored as workflow inputs.
Schedules validate timezone and recurrence, and require an existing authorized workflow
revision. Editing a workflow creates a revision; updating schedules to use it is explicit.

Schedule dispatch provides a non-null workflow_revision for saved workflows. The nullable
field on WorkflowRequest is reserved for developer-defined workflows identified by the
capability registry. The runtime must resolve the two namespaces without ambiguity.

## Persistent execution

Repositories will persist skill/workflow revisions, activation pointers, schedules,
conversation links, tool/run/step records, and outgoing message state. Today only skills
and LangGraph checkpoints are persisted; checkpoint instructions include retrieved skill
revision IDs. Dedicated execution audit tables and migrations are still pending.

Retries reuse a stable occurrence/run ID and persisted step state. Mutating tools require
operation-level duplicate protection or reconciliation. A persisted checkpoint alone cannot
guarantee an external action happened only once. Permission changes apply at execution time,
even when a skill or schedule was created earlier.

## Future generated-code lifecycle

Author immutable artifact -> validate source/dependencies -> run bounded checks in an
isolated sandbox -> register an explicitly scoped extension -> retain revision and result
history -> activate for use. Failed candidates stay inactive; rollback restores an earlier
revision. Actual sandbox provider, artifact storage, extension registry, and activation
policy remain undecided. Installing new service access may require account authorization.
Do not present generated execution as available until those components are implemented.

## Delivery sequence

Start with interactive preferences and schedules, then skill authoring/discovery and saved
workflows. Add generated-code execution only when a real use case requires it. The same API,
worker and Postgres deployment shape supports the first three levels; optional sandboxes
introduce additional hosting requirements.
