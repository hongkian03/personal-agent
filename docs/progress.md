# Conversation progress

Interactive conversations share one progress policy across skills, hosted web search,
and application tools. The CLI prints updates as they arrive; the Spectrum bridge sends
them as separate iMessages, followed by the complete answer. Scheduled morning messages
and background portfolio syncs do not receive these interactive notifications.

## Defined behavior

The default policy sends no acknowledgment when an answer finishes within five seconds.
If the request is still running, it prepares one brief acknowledgment after five seconds,
then at most one status update every 30 seconds. Up to five status updates may follow
the acknowledgment. Final answers and failures do not count toward that limit.

By default, a separate tool-free call to the configured model generates each due
notification from the current request, observed activity, shared persona, and a small
excerpt of memory already retrieved for that turn. It does not search memory again,
read the complete conversation, execute tools, or save progress text into checkpoints
or long-term memory. Each call has a three-second deadline, a 256-token output limit,
and no retries. This adds at most one small model request per notification. Fast replies
that finish before the acknowledgment delay make no progress-generation calls.

Generation runs alongside the main request and can add up to three seconds to the
notification's timing, without extending the answer deadline. Failure, timeout, invalid
or repeated wording, or a changed activity phase uses the current fixed message instead.
Waiting, retrying, and recovery notifications retain fixed wording. Finishing or cancelling
the main request cancels in-flight notification generation and prevents late updates.

Updates reflect observed activity, such as a model request running, hosted search
executing, or an application tool executing. Multiple activity changes between updates
are coalesced. If no more specific activity is available, the message simply says the
agent is still working. A search completion event does not establish that a claim has
been verified or that the whole task is finished.

Completion takes priority over pending updates. The final response is sent as a complete
message with its source links; provider text deltas are not separate iMessages. Failures
and timeouts stop the timers and produce a safe failure message. Message delivery is
best-effort: a failed status send does not prevent an attempt to send the final answer,
and ambiguous sends are not automatically retried.
The bridge bounds each send attempt to 15 seconds and typing controls to three seconds.
The messaging SDK cannot retract an in-flight send, so a timed-out send can still arrive
late; the bridge proceeds without retrying it.

## Configuration

Set these values in the root development environment or on the Railway **agent-api**
service, then restart or redeploy the process:

| Variable | Default | Behavior |
| --- | --- | --- |
| `PERSONAL_AGENT_PROGRESS_ENABLED` | `true` | Enables delayed acknowledgment and status messages. |
| `PERSONAL_AGENT_PROGRESS_DYNAMIC_ENABLED` | `true` | Generates personable wording; disable for fixed messages. |
| `PERSONAL_AGENT_PROGRESS_ACK_DELAY_SECONDS` | `5` | Delay before acknowledging a request that is still running. |
| `PERSONAL_AGENT_PROGRESS_INTERVAL_SECONDS` | `30` | Minimum time between the acknowledgment and subsequent status messages. |
| `PERSONAL_AGENT_PROGRESS_MAX_UPDATES` | `5` | Maximum status messages after the acknowledgment; excludes the terminal message. |

Disabling progress keeps final answers and failure handling. `config/agent.md` provides
communication guidance; these runtime settings control message timing. Skills do not
need to define or implement progress behavior.

On the **spectrum** service or in the bridge's local environment, set
`PERSONAL_AGENT_TYPING_ENABLED=false` to disable typing indicators. Its default is `true`.
Typing controls and progress messages are independent. Typing failures do not prevent
replies, and actual indicator visibility depends on the provider and receiving device.

The default model-call deadline is 60 seconds and the whole-turn deadline is 180 seconds.
The model deadline includes reading its stream. Heartbeats and progress messages never
extend these deadlines. The bridge initially allows 190 seconds, then derives its run
deadline from the stream's advertised timeout plus a 10-second margin.

## HTTP event contract

`POST /chat` retains the authenticated JSON request/response contract. Clients that need
progress use `POST /chat/stream` with the same bearer token and JSON request:

```json
{"message":"Use Stock research on AMD.","conversation_id":"research"}
```

The response uses `Content-Type: text/event-stream`. Each application event has a JSON
`data` payload with `run_id`, `conversation_id`, and an increasing `sequence` number.

| SSE event | Additional data | Meaning |
| --- | --- | --- |
| `started` | `timeout_seconds` | The run was accepted; supplies its execution deadline. |
| `acknowledgment` | `text` | Delayed initial acknowledgment. |
| `status` | `text` | Rate-limited activity message. |
| `result` | `text` | Terminal completed response. |
| `error` | `text`, `code` | Terminal safe failure information. |

SSE comments are transport heartbeats and must not be displayed. Clients should associate
events with the run, ignore duplicate sequence numbers, and stop after a terminal event.
Each connected run produces one terminal event. Authentication and malformed requests
can fail with ordinary HTTP errors before streaming begins; failures after streaming
begins use `error` events because the HTTP response has already started.

If the connection ends without a terminal event, the client should report the connection
loss. It must not replay the request automatically: application tools may already have
completed. Disconnecting cancels remaining request work and closes the provider stream;
it does not undo completed tools. Send `/retry` to retry an interrupted turn, or send a
new question to continue with the existing conversation history. See
[failure recovery](recovery.md) for retry boundaries and uncertain tool outcomes.

These streams are live delivery, without durable event storage or reconnection replay.
Generic progress messages are not appended to model history or saved in checkpoints.
Raw provider reasoning, tool arguments/results, credentials, and search queries are not
included in user-facing status payloads.

API warning logs include the application-authored failure message, exception type,
underlying cause type, and source file/line locations. Streaming failures also include
the run and conversation IDs so they can be matched to model/tool execution logs.
Unexpected exception strings, provider response bodies, and full tracebacks are omitted
because they may contain private request data. For example, a `ModelError` warning can
now distinguish an incomplete response from an empty reply or an interrupted stream;
its cause type and location help identify SDK, network, or application failures.
Model failures also include structured codes, safe response/incomplete status, request
and response IDs when available, HTTP status, and observed hosted-tool usage.

Recovery preserves conversation and execution state; it does not reconnect an old SSE
stream. Each `/retry` request opens its own run and progress stream. Bounded automatic
model retries remain inside the original request and its execution deadlines.

## Deployment and checks

Deploy the API before the updated bridge so `/chat/stream` is available when the bridge
switches to it. Existing JSON clients can continue using `/chat`. Both services need
their existing API token configuration; streaming adds no credentials or database schema.

After deployment, check a fast conversation for a single answer, a longer request for a
delayed acknowledgment and spaced updates, and a failure for an explicit message. Verify
that the finished research report still has readable source links. Check typing on an
actual device with its bridge setting enabled and disabled. A healthy API alone does
not establish provider access or successful iMessage delivery.
