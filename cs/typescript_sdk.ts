/**
 * Claude Stream TypeScript client (runtime: Node 18+, Bun, Deno, or any browser with fetch).
 *
 *   const cs = new ClaudeStream({ apiKey: "csk_...", baseUrl: "http://127.0.0.1:8787" });
 *   for await (const ev of cs.stream({ model: "MODEL_ID", messages: [{ role: "user", content: "Hello" }] })) {
 *     if (ev.type === "content_block_delta" && ev.data.delta?.type === "text_delta") process.stdout.write(ev.data.delta.text);
 *   }
 */

export interface StreamEvent {
  /** Upstream event name (message_start, content_block_delta, ...) or gateway.error / gateway.cancelled. */
  type: string;
  /** Event id within the run; pass it back as `afterId` to resume. */
  id?: string;
  /** Parsed payload, exactly as the upstream sent it for upstream events. */
  data: any;
}

export interface StreamParams {
  model: string;
  messages: unknown[];
  maxTokens?: number;
  effort?: "low" | "medium" | "high";
  conversationId?: string;
  idempotencyKey?: string;
  extra?: Record<string, unknown>;
}

export class ClaudeStreamError extends Error {
  constructor(public readonly status: number, message: string) {
    super(`[${status}] ${message}`);
  }
}

export class ClaudeStream {
  private readonly key: string;
  private readonly base: string;
  lastRunId?: string;
  lastConversationId?: string;

  constructor(opts: { apiKey: string; baseUrl?: string }) {
    this.key = opts.apiKey;
    this.base = (opts.baseUrl ?? "http://127.0.0.1:8787").replace(/\/$/, "");
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return { "x-api-key": this.key, "anthropic-version": "2023-06-01", ...extra };
  }

  private async fail(res: Response): Promise<never> {
    let message = res.statusText;
    try {
      const body = await res.json();
      message = body?.error?.message ?? body?.detail ?? message;
    } catch {
      /* not JSON */
    }
    throw new ClaudeStreamError(res.status, String(message));
  }

  async models(): Promise<any[]> {
    const res = await fetch(`${this.base}/v1/models`, { headers: this.headers() });
    if (!res.ok) await this.fail(res);
    return (await res.json()).data;
  }

  async *stream(params: StreamParams): AsyncGenerator<StreamEvent> {
    const body: Record<string, unknown> = {
      ...(params.extra ?? {}),
      model: params.model,
      messages: params.messages,
      max_tokens: params.maxTokens ?? 1024,
      stream: true,
    };
    if (params.effort) body.output_config = { effort: params.effort };
    if (params.conversationId) body.conversation_id = params.conversationId;

    const headers = this.headers({ "content-type": "application/json" });
    if (params.idempotencyKey) headers["idempotency-key"] = params.idempotencyKey;

    const res = await fetch(`${this.base}/v1/messages`, { method: "POST", headers, body: JSON.stringify(body) });
    if (!res.ok || !res.body) await this.fail(res);
    this.lastRunId = res.headers.get("x-run-id") ?? undefined;
    this.lastConversationId = res.headers.get("x-conversation-id") ?? undefined;
    yield* parseSSE(res.body!);
  }

  /** Re-attach to a run; events after `afterId` are replayed, then live events follow. */
  async *replay(runId: string, afterId = 0): AsyncGenerator<StreamEvent> {
    const res = await fetch(`${this.base}/api/runs/${runId}/events`, {
      headers: this.headers({ "last-event-id": String(afterId) }),
    });
    if (!res.ok || !res.body) await this.fail(res);
    yield* parseSSE(res.body!);
  }

  async cancel(runId: string): Promise<void> {
    const res = await fetch(`${this.base}/api/runs/${runId}/cancel`, { method: "POST", headers: this.headers() });
    if (!res.ok) await this.fail(res);
  }

  async conversations(): Promise<any[]> {
    const res = await fetch(`${this.base}/api/conversations`, { headers: this.headers() });
    if (!res.ok) await this.fail(res);
    return (await res.json()).data;
  }

  async uploadArtifact(name: string, content: Blob | ArrayBuffer, conversationId?: string): Promise<any> {
    const q = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : "";
    const res = await fetch(`${this.base}/api/artifacts${q}`, {
      method: "POST",
      headers: this.headers({ "content-type": "application/octet-stream", "x-filename": encodeURIComponent(name) }),
      body: content,
    });
    if (!res.ok) await this.fail(res);
    return res.json();
  }
}

/** Parses an SSE byte stream. Decoding is streaming and UTF-8 safe across chunk boundaries. */
export async function* parseSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let id: string | undefined;
  let data: string[] = [];

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      let line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line === "") {
        if (data.length) yield { type: event, id, data: JSON.parse(data.join("\n")) };
        event = "message";
        id = undefined;
        data = [];
      } else if (line.startsWith(":")) {
        continue;
      } else if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("id:")) {
        id = line.slice(3).trim();
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).replace(/^ /, ""));
      }
    }
  }
}
