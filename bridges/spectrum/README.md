# spectrum

A [Spectrum](https://photon.codes/docs/spectrum-ts) project. Wired with: imessage.

## Typing indicator

For authorized incoming text DMs, the bridge starts typing before requesting an agent
response and stops after sending the reply. It also attempts to stop on errors and
timeouts. Typing controls are best-effort; a provider failure does not block the reply.
Device visibility should be checked in an actual iMessage conversation.
Set `PERSONAL_AGENT_TYPING_ENABLED=false` to disable typing; the default is `true`.

## Conversation progress

Authorized conversations use the agent's authenticated `POST /chat/stream` endpoint.
The bridge sends delayed acknowledgments and occasional status updates, then one complete
reply or an explicit failure message. Timing is configured globally on the API; see
[conversation progress](../../docs/progress.md). Typing can remain enabled or disabled
independently of those messages. Morning message polling keeps its existing behavior.

Progress sends are best-effort, and pending updates are coalesced so they cannot form an
unbounded queue behind a slow send. If the stream ends without a final result or error,
the bridge reports a connection loss. It does not automatically replay the request or
retry ambiguous sends. The bridge uses the API's advertised run timeout plus a delivery
margin after receiving the stream's initial event.

## Environment

Before running, open `.env` and fill in the values:

From your project Settings on the [Photon dashboard](https://app.photon.codes):

- `PROJECT_ID`
- `PROJECT_SECRET`

The agent connection also requires `PERSONAL_AGENT_API_URL`, `PERSONAL_AGENT_API_TOKEN`,
and `PERSONAL_AGENT_ALLOWED_SENDER_ID`; see `.env.example`. Progress timing belongs on
the Python API, while `PERSONAL_AGENT_TYPING_ENABLED` belongs on this bridge.

## Run

```sh
npm install
npm run start
```

## Where to go next

- [Spectrum docs](https://photon.codes/docs/spectrum-ts)
- Edit `src/index.ts` for provider configuration and authorized message handling.
- Add more providers from `spectrum-ts/providers/*`.
