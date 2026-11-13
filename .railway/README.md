# Railway infrastructure

`railway.ts` describes the agent, Spectrum, and Cognee services and their persistent volumes. Service secrets
are unmanaged and remain in Railway Variables.

Use Node 22.6+ and the authenticated Railway CLI:

```sh
npm ci --prefix .railway
railway config plan
```

Review changes before running `railway config apply`. Source uploads are separate:
see [deployment and local migration instructions](../docs/railway.md).
The Cognee service uses its own `services/cognee/` upload root; see
[long-term memory setup](../docs/memory.md).
