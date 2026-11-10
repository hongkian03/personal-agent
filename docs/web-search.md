# Web search

The conversational agent can use OpenAI's hosted `web_search` tool alongside the
application's portfolio and saved-skill tools. The OpenAI adapter supplies the native
tool, preserves its output in conversation history, and formats URL citations as readable
source links for text replies, including iMessage. There is no separate search service,
API key, or browser process to run.

Search is available during interactive conversations. Morning encouragement and the
deterministic portfolio briefing do not receive the hosted search tool. Enabling search
does not make every response search the web: the model chooses when to use it. Request
search explicitly when you want the answer checked against current sources.

## Configuration

Set these values in the root `.env` for local development, or on Railway's **agent-api**
service for the hosted agent:

| Variable | Default | Behavior |
| --- | --- | --- |
| `PERSONAL_AGENT_WEB_SEARCH_ENABLED` | `true` | Makes hosted web search available to chat; `false` disables it. |
| `PERSONAL_AGENT_WEB_SEARCH_CONTEXT_SIZE` | `medium` | Search context size: `low`, `medium`, or `high`. |
| `PERSONAL_AGENT_WEB_SEARCH_MAX_CALLS` | `3` | Requested budget of built-in search, page-open, and page-find calls across one chat turn; accepts `1`–`10`. |

Use the existing `OPENAI_API_KEY` or `PERSONAL_AGENT_MODEL_API_KEY` and a model that
supports the Responses API's `web_search` tool. The application default is `gpt-6-luna`.
If you choose another model, check its tool support; an unsupported model or provider
error is not silently retried with search removed.

Environment variables override the defaults in `src/personal_agent/config.py`. Restart
the local process after editing configuration. A local `.env` edit does not update
Railway: change the hosted variables and deploy or restart that service separately.

The search budget resets for a new ordinary chat turn and is separate from
`PERSONAL_AGENT_MAX_TOOL_ROUNDS`, which bounds the application's model/function-tool
loop. A hosted response can search, open a page, or find text within a page. After the
budget is consumed, subsequent model calls in that turn do not receive the hosted tool.
OpenAI can return additional records for attempted or unfinished calls; the application
counts all returned records conservatively when deciding whether to offer search again.
This budget is not a strict count of billable queries or a monetary spending limit.
An explicit `/retry` preserves the failed turn's saved search budget and completed tool
results. Automatic provider retries stop once hosted-tool activity has been observed,
so they do not intentionally replay observed search activity. An explicit retry may need
another search when an interrupted provider response never returned reusable results;
it still uses the turn's remaining search budget.

Longer research can need a larger `PERSONAL_AGENT_MAX_OUTPUT_TOKENS` budget: reasoning
tokens and the visible answer share that limit. `PERSONAL_AGENT_MODEL_TIMEOUT_SECONDS`
bounds each model request, including its stream, while `PERSONAL_AGENT_CHAT_TIMEOUT_SECONDS`
bounds the whole turn. Streaming and progress messages do not extend either deadline.
The Spectrum bridge starts with a 190-second connection deadline, then uses the timeout
advertised by the accepted run plus a delivery margin. See [conversation progress](progress.md).

An output-limit failure is reported separately from temporary provider/network failures.
The runtime does not automatically raise `PERSONAL_AGENT_MAX_OUTPUT_TOKENS`; adjust it
explicitly when the report needs more reasoning/output space, then use `/retry` in the
same conversation. See [failure recovery](recovery.md) for the shared retry settings.

## Try it

After restarting or deploying, send a message such as:

- “Search the web for the latest official Python release announcement and link the source.”
- “Look up the ticker for this company and check its exchange and currency.”
- “Check my IBKR holdings, then search for recent official announcements from one of
  those companies. Keep the report date and the announcement date separate.”

For a basic check, use a public-information question that does not require portfolio
access. Verify that the reply contains usable source links and that the linked pages
support the claims. A portfolio question may combine cached account data with current
public research; the answer should distinguish the sources and their dates.

Web search is not a guaranteed real-time quote feed. Search results may contain delayed
prices or older pages. The existing IBKR Flex price tool still returns report-date
closing prices, and web search does not enable broker login, trading, or order placement.

## Implementation and costs

The hosted integration lives in `src/personal_agent/integrations/llm/`. It uses OpenAI's
native `web_search` tool rather than an application function named `web.search`.
Search output remains in the model's history in its original provider format; citation
formatting changes the visible reply only. This is web retrieval, not general browser
automation or access to signed-in websites.

The adapter consumes the Responses API stream and reports coarse search activity to the
shared progress policy. Individual search completion does not mean the whole conversation
has finished or a financial claim has been verified. Complete provider outputs still
supply citations, function calls, and search-budget accounting; partial text is not sent
as separate iMessages. The same progress policy applies to all interactive requests,
including requests that never search the web.

OpenAI bills search usage in addition to model usage. Search context size, the configured
call budget, and the number of chat turns affect usage. Consult OpenAI's current
[web-search documentation](https://developers.openai.com/api/docs/guides/tools-web-search)
and [pricing](https://developers.openai.com/api/docs/pricing) for supported models,
features, and current charges.
