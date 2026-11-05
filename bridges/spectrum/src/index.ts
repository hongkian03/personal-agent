import { createHash } from "node:crypto";
import { imessage } from "@spectrum-ts/imessage";
import { Spectrum } from "spectrum-ts";

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Set ${name} in the Spectrum bridge's .env`);
  return value;
}

const allowedSender = requiredEnv("PERSONAL_AGENT_ALLOWED_SENDER_ID").toLowerCase();
const apiToken = requiredEnv("PERSONAL_AGENT_API_TOKEN");
const chatUrl = new URL("/chat", process.env.PERSONAL_AGENT_API_URL ?? "http://127.0.0.1:8000");
const projectId = requiredEnv("PROJECT_ID");
const app = await Spectrum({
  projectId,
  projectSecret: requiredEnv("PROJECT_SECRET"),
  providers: [imessage.config()],
});

console.log("Spectrum bridge is listening for authorized iMessages.");

for await (const [space, message] of app.messages) {
  if (
    message.direction !== "inbound" ||
    message.sender?.id.toLowerCase() !== allowedSender ||
    !("type" in space) || space.type !== "dm" ||
    message.content.type !== "text"
  ) continue;

  const text = message.content.text.trim();
  if (!text) continue;

  const conversationId = "imessage-" + createHash("sha256")
    .update(JSON.stringify([projectId, space.id, allowedSender]))
    .digest("hex");

  try {
    const response = await fetch(chatUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
      redirect: "error",
      signal: AbortSignal.timeout(190_000),
    });
    if (!response.ok) {
      console.error(`Agent API returned HTTP ${response.status}`);
      continue;
    }
    const reply: unknown = await response.json();
    if (
      typeof reply !== "object" || reply === null ||
      !("text" in reply) || typeof reply.text !== "string" || !reply.text.trim()
    ) {
      throw new Error("Agent API returned an empty or invalid reply");
    }
    await space.send(reply.text);
  } catch (error) {
    console.error("Could not reply to iMessage:", error instanceof Error ? error.name : "unknown error");
  }
}
