import assert from "node:assert/strict";
import test from "node:test";
import { ChatDelivery, ChatStreamError, readChatEvents } from "./chat-stream.ts";
import type { ChatEvent } from "./chat-stream.ts";

function event(kind: ChatEvent["kind"], sequence: number, text: string = kind): ChatEvent {
  return { kind, sequence, text, run_id: "run-1", conversation_id: "chat-1",
    ...(kind === "started" ? { timeout_seconds: 180 } : {}),
    ...(kind === "error" ? { code: "failed" } : {}) };
}

function frame(value: ChatEvent): string {
  const { kind, ...data } = value;
  return `event: ${kind}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
}

function response(text: string, chunkSize = 17): Response {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (let offset = 0; offset < bytes.length; offset += chunkSize) {
        controller.enqueue(bytes.slice(offset, offset + chunkSize));
      }
      controller.close();
    },
  }), { headers: { "content-type": "text/event-stream; charset=utf-8" } });
}

async function collect(value: Response): Promise<ChatEvent[]> {
  const result: ChatEvent[] = [];
  for await (const item of readChatEvents(value, "chat-1")) result.push(item);
  return result;
}

test("SSE handles byte-fragmented UTF-8, CRLF, heartbeats and duplicate sequences", async () => {
  const started = event("started", 0);
  const acknowledgment = event("acknowledgment", 1, "I’m searching. ☕");
  const result = event("result", 2, "Completed report.");
  const items = await collect(response(": heartbeat\r\n\r\n" + frame(started) +
    frame(acknowledgment) + frame(acknowledgment) + frame(result), 1));
  assert.deepEqual(items, [started, acknowledgment, result]);
});

test("SSE supports multiline JSON data and stops at its first terminal event", async () => {
  const started = event("started", 0);
  const result = event("result", 1, "Answer.");
  const { kind, ...data } = result;
  const multiline = JSON.stringify(data, null, 2).split("\n").map(line => `data: ${line}`).join("\n");
  const items = await collect(response(frame(started) + `event: ${kind}\n${multiline}\n\n` +
    frame(event("error", 2, "Should not be delivered."))));
  assert.deepEqual(items, [started, result]);
});

test("EOF without a terminal event reports connection loss", async () => {
  await assert.rejects(collect(response(frame(event("started", 0)) +
    frame(event("status", 1, "Working.")))), /lost the connection/);
});

test("failed response becomes a terminal event", async () => {
  const error = event("error", 1, "The request timed out.");
  assert.deepEqual(await collect(response(frame(event("started", 0)) + frame(error))),
    [event("started", 0), error]);
});

test("invalid event contracts are rejected without exposing their data", async () => {
  const started = frame(event("started", 0));
  const invalid = [
    frame(event("result", 0)),
    started + "event: status\ndata: sensitive-invalid-json\n\n",
    started + frame({ ...event("status", 1), run_id: "another-run" }),
    started + frame({ ...event("status", 1), conversation_id: "another-chat" }),
    started + frame({ ...event("status", 1), text: " " }),
    frame({ ...event("started", 0), timeout_seconds: 999999 }),
  ];
  for (const text of invalid) {
    await assert.rejects(collect(response(text)), (error: unknown) =>
      error instanceof ChatStreamError && !error.message.includes("sensitive"));
  }
});

test("HTTP errors never forward private response bodies", async () => {
  for (const status of [400, 401, 422, 502, 504]) {
    await assert.rejects(collect(new Response("private provider details", { status })),
      (error: unknown) => error instanceof ChatStreamError && !error.message.includes("private"));
  }
});

test("delivery coalesces progress while a send is pending and prioritizes final output", async () => {
  const sent: string[] = [];
  let release: () => void = () => {};
  const blocked = new Promise<void>(resolve => { release = resolve; });
  const queue = new ChatDelivery(async text => {
    sent.push(text);
    if (sent.length === 1) await blocked;
  }, () => assert.fail("Unexpected send failure"));
  queue.push(event("acknowledgment", 1, "Acknowledged."));
  queue.push(event("status", 2, "Old progress."));
  queue.push(event("status", 3, "Latest progress."));
  assert.deepEqual(sent, ["Acknowledged."]);
  queue.push(event("result", 4, "Final answer."));
  queue.push(event("status", 5, "Late progress."));
  queue.finishWith("Duplicate failure.");
  release();
  await queue.finished();
  assert.deepEqual(sent, ["Acknowledged.", "Final answer."]);
});

test("progress send failure does not prevent the final attempt or trigger a retry", async () => {
  const sent: string[] = [];
  const errors: boolean[] = [];
  const queue = new ChatDelivery(async (text, terminal) => {
    sent.push(text);
    if (!terminal) throw new Error("Ambiguous provider send");
  }, (_error, terminal) => errors.push(terminal));
  queue.push(event("acknowledgment", 1, "Working."));
  queue.push(event("result", 2, "Answer."));
  await queue.finished();
  assert.deepEqual(sent, ["Working.", "Answer."]);
  assert.deepEqual(errors, [false]);
});

test("an event arriving between drain completion and worker cleanup is delivered", async () => {
  const sent: string[] = [];
  const queue = new ChatDelivery(async (text, terminal) => {
    sent.push(text);
    if (!terminal) throw new Error("Progress failed");
  }, () => {
    // This runs after drain's final iteration but before its finally reaction.
    queueMicrotask(() => queue.finishWith("Final answer."));
  });
  queue.push(event("acknowledgment", 1, "Working."));
  await queue.finished();
  assert.deepEqual(sent, ["Working.", "Final answer."]);
});

test("a hanging progress send is bounded and the terminal message still gets one attempt", async () => {
  const sent: string[] = [];
  const errors: boolean[] = [];
  const queue = new ChatDelivery(async (text, terminal) => {
    sent.push(text);
    if (!terminal) await new Promise<void>(() => {});
  }, (_error, terminal) => errors.push(terminal), 10);
  queue.push(event("acknowledgment", 1, "Working."));
  queue.finishWith("Final answer.");
  await queue.finished();
  assert.deepEqual(sent, ["Working.", "Final answer."]);
  assert.deepEqual(errors, [false]);
});

test("a hanging final send is bounded and is never retried", async () => {
  const sent: string[] = [];
  const errors: boolean[] = [];
  const queue = new ChatDelivery(async text => {
    sent.push(text);
    await new Promise<void>(() => {});
  }, (_error, terminal) => errors.push(terminal), 10);
  queue.finishWith("Final answer.");
  await queue.finished();
  queue.finishWith("Retry must be ignored.");
  await queue.finished();
  assert.deepEqual(sent, ["Final answer."]);
  assert.deepEqual(errors, [true]);
});
