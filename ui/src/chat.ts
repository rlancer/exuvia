/**
 * The agent's chat as the side panel draws it, and the saved transcript
 * (`GET /api/agent/{id}`) turned back into it, so a reload or a past chat
 * shows what was said.
 */

export type Item =
  | { kind: "user"; text: string }
  | { kind: "ai"; text: string }
  | { kind: "tool"; id: string; name: string; input?: Record<string, unknown>; ok?: boolean; summary?: string; node?: string }
  | { kind: "error"; text: string };

/** One saved event, as `agent.py` records it. */
export type SavedEvent =
  | { type: "user"; text: string; at?: number }
  | { type: "text"; text: string }
  | { type: "tool"; id: string; name: string; input?: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; ok: boolean; summary?: string; node?: string }
  | { type: "error"; error: string };

/** The chat a saved transcript draws. Events of a kind it does not know are skipped. */
export function itemsOf(events: SavedEvent[]): Item[] {
  const out: Item[] = [];
  for (const e of events) {
    const last = out[out.length - 1];
    switch (e.type) {
      case "user":
        out.push({ kind: "user", text: e.text });
        break;
      case "text":
        if (last?.kind === "ai") out[out.length - 1] = { ...last, text: last.text + e.text };
        else out.push({ kind: "ai", text: e.text });
        break;
      case "tool":
        out.push({ kind: "tool", id: e.id, name: e.name, input: e.input });
        break;
      case "tool_result": {
        let i = out.length - 1;
        while (i >= 0 && !(out[i].kind === "tool" && (out[i] as { id: string }).id === e.id)) i--;
        const result = { ok: e.ok, summary: e.summary, node: e.node };
        if (i >= 0) out[i] = { ...(out[i] as Extract<Item, { kind: "tool" }>), ...result };
        else out.push({ kind: "tool", id: e.id, name: e.name, ...result });
        break;
      }
      case "error":
        out.push({ kind: "error", text: e.error });
        break;
    }
  }
  return out;
}

/** The project's chat this browser had open last, kept per project. */
export function chatKey(apiBase: string): string {
  return `exuvia-chat:${apiBase}`;
}
