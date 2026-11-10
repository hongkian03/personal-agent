# Conversation failure recovery

Interactive failures preserve the conversation and its completed work. A failed model
request, output-token limit, timeout, or disconnected client does not require clearing
all prior context.

## Conversation controls

| Message | Behavior |
| --- | --- |
| `/retry` | Retry the saved failed turn at its unfinished step, reusing completed tool results. |
| A new ordinary question | Close the previous failed attempt and begin a new turn with the existing conversation history. |
| `/new` | Intentionally clear this conversation's checkpoints and start fresh. Saved skills and portfolio data are preserved. |
| `/recovery` | List uncertain actions by tool, operation ID, and safe skill/revision summary, without displaying saved instructions or raw arguments. |
| `/resolve <operation-id> applied` | Record that you checked the action's state and confirmed it happened. |
| `/resolve <operation-id> not-applied` | Record that you checked the action's state and confirmed it did not happen, allowing a later explicit retry. |

Send these controls through iMessage, the CLI, or the existing authenticated chat API.
`/retry` is handled by the runtime; it is not added to the model's history as a new
research question. Retrying uses the original request to load the appropriate saved
skill guidance. If there is no unfinished turn, `/retry` returns a short explanation
without calling the model.

A retry retains the failed turn's tool-round and hosted-search budgets. A new ordinary
turn starts with new budgets. Completed tool results and provider history remain intact;
partial model output and incomplete function-call arguments are never executed as tools.

## Tool receipts and uncertain outcomes

Before executing an application tool, the runtime records a durable operation receipt.
Completed outputs are reused when the failed turn resumes. An interrupted read can run
again, but a write that started without a recorded result has an uncertain outcome:
the external or saved state may already have changed.

For saved-skill writes, recovery checks database evidence against the expected saved
revision/content or activation/archive target. An exact match can reconcile the action
without repeating it. Missing or different evidence remains uncertain. An old checkpoint
that predates receipts is handled conservatively: pending writes are not executed merely
because no receipt exists.

An uncertain write blocks the matching tool and arguments across later turns until the
action is reconciled. Other conversation work can continue. Use `/recovery` to find its
operation ID, then inspect the actual saved state before resolving it:

```text
/recovery
/resolve <operation-id> applied
```

If the action definitely did not happen, use `not-applied` instead, followed by `/retry`
when you want to try the failed turn again. `/resolve` records your explicit confirmation;
it does not execute the action. Confirming `applied` supplies an owner-confirmed result
so recovery can proceed even when the original tool output is unavailable. Use either
the full 64-character operation ID or a unique prefix of at least 12 characters.

Receipts use the configured persistence backend: the local `skills.sqlite` database or
PostgreSQL. They are separate from conversation checkpoints. `/new` does not delete
receipts or clear unresolved action uncertainty, so resetting a conversation cannot
silently permit a duplicate write.

## Automatic model retries

Temporary provider/network failures can trigger a bounded retry of the model request.
Authentication errors, unsupported requests, output-token exhaustion, and unexpected
processing failures are not automatically retried. Once hosted-tool activity has been
observed, the adapter does not automatically repeat that request: a search may already
have run even if its final response was lost.

| Variable | Default | Behavior |
| --- | --- | --- |
| `PERSONAL_AGENT_MODEL_MAX_ATTEMPTS` | `2` | Maximum attempts per model request, including the first; accepts `1`–`3`. |
| `PERSONAL_AGENT_MODEL_RETRY_DELAY_SECONDS` | `0.5` | Delay between eligible attempts; accepts `0`–`10` seconds. |
| `PERSONAL_AGENT_MODEL_TIMEOUT_SECONDS` | `60` | One wall-clock deadline shared by all attempts and their streams. |
| `PERSONAL_AGENT_CHAT_TIMEOUT_SECONDS` | `180` | Whole incoming-request deadline, including locks, tools, and all model requests. |

Set these variables on the API process and restart or redeploy it. There is no second
layer of SDK retries that multiplies the configured attempt count. Retries do not change
the selected model, remove web search, or automatically increase token limits.

An explicit `/retry` is a new incoming request with a new request deadline. It resumes
the failed turn's execution state, so its prior tool results and budgets still apply.
If the failure was an output limit, adjust `PERSONAL_AGENT_MAX_OUTPUT_TOKENS` explicitly
before trying again. Increasing that limit permits more generated reasoning and output;
it does not guarantee that a request will complete within the time deadline.

## Connection and delivery failures

Disconnecting closes the active provider stream and cancels remaining request work.
It does not undo completed tools. The next `/retry` or new question can recover using
the saved execution state, including after an API restart when persistent storage is
available.

An old SSE stream cannot be reconnected or replayed. Each recovery request creates a
new stream with a new run ID. The bridge never automatically replays a request after
connection loss, and a failed or ambiguous iMessage send is not retried automatically.
Execution recovery and message-delivery confirmation are separate concerns.

## Checks after deployment

Start with a successful conversation, then simulate a model failure and send `/retry`.
Verify that earlier context and completed tool results remain available and that the
original question is retried. After another failure, send a different question and
verify that it proceeds without `/new`. Check an output-limit failure for an explicit
budget explanation and confirm that it does not silently raise the budget.
For a write interrupted after execution but before its result is saved, verify that
recovery reuses a reconciled result or lists the action as uncertain. It must not repeat
the write automatically. Check `/recovery` and the explicit resolution controls with
synthetic saved-state data before relying on them for an actual ambiguous action.

Use API logs to match run IDs across retries and tool execution. A completed response
still includes readable source links, and a failed request still produces a terminal
error when the transport is connected. A healthy API alone does not establish that
provider credentials or iMessage delivery work.
