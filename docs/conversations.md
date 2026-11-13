# Conversation history and persona changes

The agent loads `config/agent.md` for each model-generated reply. Its current name and
communication preferences take precedence over older conversation references. On Railway,
local file edits must be deployed before the hosted API can read them.

## Start fresh

- **iMessage:** send `/new` as a standalone message from the configured allowed sender.
  The API deletes the checkpoints for that conversation and replies with a confirmation.
  Your next message uses a fresh context with the current persona.
- **HTTP API:** authenticated `POST /chat` with `message: "/new"` and the existing
  `conversation_id` resets that conversation. Omitting `conversation_id` on an ordinary
  message instead creates a separate new conversation and retains the old one.
- **CLI:** `/new` starts a separate conversation without deleting the previous saved
  conversation. Starting the CLI without `--conversation` also starts fresh.

The iMessage/API reset deletes checkpoints and pending checkpoint writes for the selected
owner-scoped conversation. Other conversations, saved skills, IBKR snapshots, persona
configuration, and morning delivery records are preserved. It does not delete the message
history displayed in Apple's Messages app.

When [long-term memory](memory.md) is enabled, resets also preserve Cognee memories and
queued ingestion. A fresh conversation can therefore recall information from earlier
ones. Use the memory tools to search and forget individual sources separately.

Morning encouragement uses the current shared persona and recent morning messages for
variety. When memory is enabled, it also retrieves relevant Cognee context about goals,
projects, priorities, and communication preferences. It does not load a conversation's
checkpoint history directly. Resetting a chat preserves both recent morning messages
and long-term memory.
