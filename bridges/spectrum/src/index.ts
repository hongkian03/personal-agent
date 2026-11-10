import { createHash } from "node:crypto";
import { setInterval } from "node:timers/promises";
import { imessage } from "@spectrum-ts/imessage";
import { Spectrum, markdown } from "spectrum-ts";
import {
  ChatDelivery, ChatStreamError, CONNECTION_LOST, REQUEST_TIMED_OUT,
  readChatEvents, withDeadline,
} from "./chat-stream.ts";

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Set ${name} in the Spectrum bridge's .env`);
  return value;
}

const allowedSender = requiredEnv("PERSONAL_AGENT_ALLOWED_SENDER_ID").toLowerCase();
const apiToken = requiredEnv("PERSONAL_AGENT_API_TOKEN");
const chatUrl = new URL("/chat/stream", process.env.PERSONAL_AGENT_API_URL ?? "http://127.0.0.1:8000");
const typingEnabled = process.env.PERSONAL_AGENT_TYPING_ENABLED !== "false";
const projectId = requiredEnv("PROJECT_ID");
const app = await Spectrum({
  projectId,
  projectSecret: requiredEnv("PROJECT_SECRET"),
  providers: [imessage.config()],
});

async function sendMorningMessages(): Promise<void> {
  const provider = imessage(app);
  const claims = [
    ...(process.env.PERSONAL_AGENT_PORTFOLIO_MORNING_ENABLED !== "false"
      ? [{ path: "/portfolio/morning/claim", label: "Portfolio briefing" }] : []),
    ...(process.env.PERSONAL_AGENT_MORNING_ENABLED === "true"
      ? [{ path: "/morning/claim", label: "Morning message" }] : []),
  ];
  let dm: Awaited<ReturnType<typeof provider.space.create>> | undefined;

  for await (const _ of setInterval(30_000, undefined, { ref: false })) {
    for (const claim of claims) {
      try {
        // Resolve the approved recipient before reserving a day's message.
        dm ??= await provider.space.create(allowedSender);
        const response = await fetch(new URL(claim.path, chatUrl), {
          method: "POST",
          headers: { Authorization: `Bearer ${apiToken}` },
          redirect: "error",
          signal: AbortSignal.timeout(190_000),
        });
        if (response.status === 204) continue;
        if (!response.ok) {
          console.error(`${claim.label} API returned HTTP ${response.status}`);
          continue;
        }
        const reply: unknown = await response.json();
        if (
          typeof reply !== "object" || reply === null ||
          !("text" in reply) || typeof reply.text !== "string" || !reply.text.trim()
        ) {
          throw new Error("Morning message API returned an invalid message");
        }
        await dm.send(markdown(reply.text));
        console.log(`${claim.label} submitted to Spectrum.`);
      } catch (error) {
        console.error("Morning message failed:", error instanceof Error ? error.name : "unknown error");
      }
    }
  }
}

if (
  process.env.PERSONAL_AGENT_MORNING_ENABLED === "true" ||
  process.env.PERSONAL_AGENT_PORTFOLIO_MORNING_ENABLED !== "false"
) {
  void sendMorningMessages();
}

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

  const controller = new AbortController();
  let deadline = setTimeout(() => controller.abort(), 190_000);
  const startTyping = async (): Promise<void> => {
    if (typingEnabled) await withDeadline(space.startTyping(), 3000).catch(() => {
      console.warn("Could not start iMessage typing indicator.");
    });
  };
  const delivery = new ChatDelivery(async (reply, terminal) => {
    await space.send(markdown(reply));
    // Sending an interim message can clear typing on the recipient's device.
    if (!terminal && !delivery.hasTerminal) await startTyping();
  }, (_error, terminal) => {
    console.error(`Could not send ${terminal ? "agent reply" : "progress message"} to iMessage.`);
  });

  try {
    // Typing is best-effort: provider failures must not prevent an agent reply.
    await startTyping();
    const response = await fetch(chatUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiToken}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
      redirect: "error",
      signal: controller.signal,
    });
    for await (const event of readChatEvents(response, conversationId)) {
      if (event.kind === "started") {
        clearTimeout(deadline);
        // Follow the API's whole-turn deadline, with room for its terminal event.
        deadline = setTimeout(() => controller.abort(), (event.timeout_seconds! + 10) * 1000);
      }
      delivery.push(event);
    }
  } catch (error) {
    console.error("Could not reply to iMessage:", error instanceof Error ? error.name : "unknown error");
    delivery.finishWith(error instanceof ChatStreamError ? error.userMessage
      : controller.signal.aborted
        ? REQUEST_TIMED_OUT : CONNECTION_LOST);
  } finally {
    clearTimeout(deadline);
    await delivery.finished();
    // Also runs on HTTP errors, invalid replies, send failures, and timeouts.
    if (typingEnabled) await withDeadline(space.stopTyping(), 3000).catch(() => {
      console.warn("Could not stop iMessage typing indicator.");
    });
  }
}
