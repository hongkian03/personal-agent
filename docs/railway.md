# Railway hosting

Project: [personal-agent](https://railway.com/project/920e1ed2-70ed-4a82-b829-e0d2bc5a7d66)
in Kian Hong's Projects, environment `production`.

| Resource | Configuration |
| --- | --- |
| `agent-api` | Root Dockerfile; one process, one replica; port 8000; `/health` startup check |
| `spectrum` | `bridges/spectrum/Dockerfile`; one long-running bridge; no HTTP port |
| `agent-data` | 500 MB persistent volume mounted at `/data` on `agent-api` |
| API connection | `http://agent-api.railway.internal:8000` within the project |

The services use CLI uploads of the working tree. GitHub automatic deployments are not
connected, so uncommitted local changes can be deployed without pushing the repository.
Railway configuration is defined in [`.railway/railway.ts`](../.railway/railway.ts).
Secrets are managed separately in Railway Variables, never in that file or build archives.
`.railwayignore` and the Docker allowlists exclude local credentials and application data.

The API also supports IBKR Flex portfolio reporting. Follow [the IBKR setup guide](ibkr.md)
to create the query and set both IBKR variables on `agent-api`. Background sync, cached
portfolio tools and a separate morning portfolio briefing start using that connection;
the bridge's existing motivational message remains independently enabled.

## Required variables

On `agent-api`, set `OPENAI_API_KEY` (or `PERSONAL_AGENT_MODEL_API_KEY`) and
`PERSONAL_AGENT_API_TOKEN`. Preserve the desired `PERSONAL_AGENT_MODEL`,
`PERSONAL_AGENT_OWNER_ID`, and `PERSONAL_AGENT_TIMEZONE` from local configuration.
Set `PERSONAL_AGENT_ENVIRONMENT=production`, `PERSONAL_AGENT_DATA_DIR=/data`, and
`PORT=8000`. Leave `PERSONAL_AGENT_DATABASE_URL` unset or empty to use SQLite.

On `spectrum`, set `PROJECT_ID`, `PROJECT_SECRET`, and
`PERSONAL_AGENT_ALLOWED_SENDER_ID` from the existing Photon setup.
Set `PERSONAL_AGENT_API_TOKEN` to `${{agent-api.PERSONAL_AGENT_API_TOKEN}}` and
`PERSONAL_AGENT_API_URL=http://agent-api.railway.internal:8000`.
Preserve `PERSONAL_AGENT_MORNING_ENABLED` from the bridge configuration; enable it
with `true` to poll for the daily 8 a.m. message in the API's configured timezone.
Morning delivery is enabled on the Railway `spectrum` service. Set its
`PERSONAL_AGENT_MORNING_ENABLED` variable to `false` to disable it.

Keep only one bridge connected to the Photon project. Stop a local bridge when switching
to Railway to avoid competing message consumers.

Interactive progress timing is configured on `agent-api`, while
`PERSONAL_AGENT_TYPING_ENABLED` is configured on `spectrum`. Both default to enabled;
see [conversation progress](progress.md) for timing variables and the streaming contract.
Deploy API changes before bridge changes so `/chat/stream` is available when the updated
bridge switches to it. Existing `/chat` JSON clients remain compatible.

## Deploy source changes

From the repository root, with the Railway CLI authenticated:

```sh
railway link --project 920e1ed2-70ed-4a82-b829-e0d2bc5a7d66 --environment production --service agent-api
railway up --service agent-api --environment production --detach
railway up bridges/spectrum --path-as-root --service spectrum --environment production --detach
railway deployment list --service agent-api --environment production --json
railway deployment list --service spectrum --environment production --json
```

The bridge upload uses its own directory as the archive root. Each service's Dockerfile
therefore sees exactly the source layout it expects. `--detach` returns before a service
is healthy; check deployment status and runtime logs after uploading.

```sh
railway logs --service agent-api --lines 50
railway logs --service spectrum --lines 50
```

The API health check verifies startup and storage initialization. It does not prove that
model credentials or Photon credentials work. Verify an authenticated `/chat` request
inside Railway, then send an iMessage from the approved sender to check delivery.

## Change infrastructure

Use Node 22.6 or newer for Railway's TypeScript evaluator. Install the infrastructure
dependencies and preview changes:

```sh
npm ci --prefix .railway
railway config plan
```

Review the plan, then use `railway config apply` to apply intended infrastructure changes.
Variables are unmanaged by this file and stay in Railway. Source uploads remain separate
from infrastructure updates. The volume region is the concrete region returned by Railway;
changing it moves storage and should be reviewed separately.

## Move to a local machine later

The application uses the same Dockerfiles and SQLite format in both environments.
Railway starts with its own database; existing `.data/` files remain on this computer
unless explicitly migrated.

For a future move, stop the Railway bridge, take consistent SQLite backups of both
`/data/checkpoints.sqlite` and `/data/skills.sqlite`, then stop the Railway API.
Use SQLite's backup API rather than copying live database files. Transfer the backups
to the new host and restore them into the Compose `agent-data` volume while its API is
stopped. Preserve the owner ID, model configuration, API token, Photon credentials,
allowed sender, and timezone in the new host's environment files. Set the bridge API URL
to `http://agent-api:8000` as provided by `compose.yaml`, then run:

```sh
docker compose up -d --build
docker compose logs --tail=50 agent-api spectrum
```

Confirm chat and iMessage delivery on the new host before removing Railway resources.
Keep a separate database backup before deleting a persistent volume.
