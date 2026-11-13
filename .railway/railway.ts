import { defineRailway, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const agentData = volume("agent-data", {
    alerts: { usage: { "80": {}, "95": {}, "100": {} } },
    allowOnlineResize: true,
    region: "us-east4-eqdc4a",
    sizeMB: 500,
  });
  const cogneeData = volume("cognee-data", {
    alerts: { usage: { "80": {}, "95": {}, "100": {} } },
    allowOnlineResize: true,
    region: "us-east4",
    sizeMB: 500,
  });
  const cognee = service("cognee", {
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/health",
    healthcheckTimeout: 180,
    replicas: { "us-east4": 1 },
    deploy: { overlapSeconds: 0, drainingSeconds: 30 },
    volumeMounts: { "/cognee-storage": cogneeData },
  });
  const spectrum = service("spectrum", {
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "us-east4": 1 },
    deploy: { overlapSeconds: 0 },
  });
  const agentApi = service("agent-api", {
    source: { type: "github", repo: "hongkian03/personal-agent", branch: "main" },
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/health",
    healthcheckTimeout: 120,
    replicas: { "us-east4": 1 },
    volumeMounts: { "/data": agentData },
  });

  return project("personal-agent", {
    variables: { managed: false },
    resources: [spectrum, agentApi, agentData, cognee, cogneeData],
  });
});
