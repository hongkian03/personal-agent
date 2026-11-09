/** Validated chat SSE and ordered, coalescing message delivery. No provider credentials here. */

export type ChatEvent = {
  kind: "started" | "acknowledgment" | "status" | "result" | "error";
  run_id: string;
  conversation_id: string;
  sequence: number;
  text?: string;
  code?: string;
  timeout_seconds?: number;
};

export class ChatStreamError extends Error {
  constructor(public readonly userMessage: string) {
    super(userMessage);
    this.name = "ChatStreamError";
  }
}

const CONNECTION_LOST = "I lost the connection before receiving a complete answer. Please try again. Your request was not automatically replayed.";
const MAX_FRAME_CHARS = 1_000_000;

/** Bounds waiting for SDK calls that do not expose cancellation. Never retries a send. */
export async function withDeadline<T>(operation: Promise<T>, milliseconds: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("Message delivery timed out")), milliseconds);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

function parseEvent(name: string, data: string): ChatEvent {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    throw new ChatStreamError(CONNECTION_LOST);
  }
  if (typeof value !== "object" || value === null) throw new ChatStreamError(CONNECTION_LOST);
  const record = value as Record<string, unknown>;
  if (
    !["started", "acknowledgment", "status", "result", "error"].includes(name) ||
    typeof record.run_id !== "string" || !record.run_id || record.run_id.length > 128 ||
    typeof record.conversation_id !== "string" || !record.conversation_id || record.conversation_id.length > 128 ||
    !Number.isSafeInteger(record.sequence) || (record.sequence as number) < 0
  ) throw new ChatStreamError(CONNECTION_LOST);
  if (name === "started") {
    if (typeof record.timeout_seconds !== "number" || !Number.isFinite(record.timeout_seconds) ||
      record.timeout_seconds <= 0 || record.timeout_seconds > 1800) {
      throw new ChatStreamError(CONNECTION_LOST);
    }
  } else if (typeof record.text !== "string" || !record.text.trim()) {
    throw new ChatStreamError(CONNECTION_LOST);
  }
  if (name === "error" && (typeof record.code !== "string" || !record.code)) {
    throw new ChatStreamError(CONNECTION_LOST);
  }
  return { ...record, kind: name } as ChatEvent;
}

/** Handles UTF-8 and SSE lines split across arbitrary network chunks, including CRLF. */
export async function* readChatEvents(
  response: Response,
  conversationId: string,
): AsyncGenerator<ChatEvent> {
  if (!response.ok) {
    const text = response.status === 504
      ? "The agent request timed out. Please try again."
      : response.status === 401 || response.status === 403
        ? "The agent connection is not configured correctly."
        : response.status === 400 || response.status === 422
          ? "I couldn’t process that request. Please try a new conversation."
          : "The agent is unavailable right now. Please try again later.";
    throw new ChatStreamError(text);
  }
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream") || !response.body) {
    throw new ChatStreamError(CONNECTION_LOST);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let name = "message";
  let data: string[] = [];
  let frameChars = 0;
  let runId: string | undefined;
  let sequence = -1;
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += chunk.done ? decoder.decode() : decoder.decode(chunk.value, { stream: true });
      while (true) {
        const boundary = buffer.search(/[\r\n]/);
        if (boundary < 0) break;
        // A trailing CR might be half of CRLF in the next network chunk.
        if (buffer[boundary] === "\r" && boundary === buffer.length - 1 && !chunk.done) break;
        const line = buffer.slice(0, boundary);
        const width = buffer[boundary] === "\r" && buffer[boundary + 1] === "\n" ? 2 : 1;
        buffer = buffer.slice(boundary + width);
        frameChars += line.length;
        if (frameChars > MAX_FRAME_CHARS) throw new ChatStreamError(CONNECTION_LOST);
        if (!line) {
          if (data.length) {
            const event = parseEvent(name, data.join("\n"));
            if (event.conversation_id !== conversationId || (runId && event.run_id !== runId)) {
              throw new ChatStreamError(CONNECTION_LOST);
            }
            if (event.sequence > sequence) {
              if (!runId && event.kind !== "started") throw new ChatStreamError(CONNECTION_LOST);
              if (runId && event.kind === "started") throw new ChatStreamError(CONNECTION_LOST);
              runId = event.run_id;
              sequence = event.sequence;
              yield event;
              if (event.kind === "result" || event.kind === "error") return;
            }
          }
          name = "message";
          data = [];
          frameChars = 0;
        } else if (!line.startsWith(":")) {
          const colon = line.indexOf(":");
          const field = colon < 0 ? line : line.slice(0, colon);
          let value = colon < 0 ? "" : line.slice(colon + 1);
          if (value.startsWith(" ")) value = value.slice(1);
          if (field === "event") name = value;
          if (field === "data") data.push(value);
        }
      }
      if (buffer.length + frameChars > MAX_FRAME_CHARS) throw new ChatStreamError(CONNECTION_LOST);
      if (chunk.done) throw new ChatStreamError(CONNECTION_LOST);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

/** At most one pending progress message; a terminal message replaces unsent progress. */
export class ChatDelivery {
  private progress: string | undefined;
  private terminal: string | undefined;
  private worker: Promise<void> | undefined;
  private terminalSeen = false;

  constructor(
    private readonly send: (text: string, terminal: boolean) => Promise<void>,
    private readonly onSendError: (error: unknown, terminal: boolean) => void,
    private readonly sendTimeoutMs = 15_000,
  ) {}

  get hasTerminal(): boolean { return this.terminalSeen; }

  push(event: ChatEvent): void {
    if (event.kind === "started" || this.terminalSeen) return;
    if (event.kind === "result" || event.kind === "error") {
      this.finishWith(event.text!);
    } else {
      this.progress = event.text!;
      this.startWorker();
    }
  }

  finishWith(text: string): void {
    if (this.terminalSeen) return;
    this.terminalSeen = true;
    this.progress = undefined;
    this.terminal = text;
    this.startWorker();
  }

  private startWorker(): void {
    this.worker ??= this.drain().finally(() => {
      this.worker = undefined;
      // An event can arrive after drain returns but before this finally callback runs.
      if (this.terminal !== undefined || this.progress !== undefined) this.startWorker();
    });
  }

  private async drain(): Promise<void> {
    while (this.terminal !== undefined || this.progress !== undefined) {
      const terminal = this.terminal !== undefined;
      const text = terminal ? this.terminal! : this.progress!;
      if (terminal) this.terminal = undefined;
      else this.progress = undefined;
      try {
        await withDeadline(this.send(text, terminal), this.sendTimeoutMs);
      } catch (error) {
        // An ambiguous send must not be retried; final delivery still gets its own attempt.
        try { this.onSendError(error, terminal); } catch { /* Logging must not break delivery. */ }
      }
    }
  }

  async finished(): Promise<void> {
    // A settled worker may have restarted to deliver an event received during cleanup.
    while (this.worker !== undefined) await this.worker;
  }
}
