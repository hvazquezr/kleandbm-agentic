/**
 * Streaming chat with an agent. The backend answers POST .../messages with Server-Sent Events;
 * openapi-fetch doesn't stream, so this uses fetch directly with the same auth.
 */
import { ApiError, authHeaders } from "./client";

/** An agent that answers a routed turn, or the router itself. */
export type TurnAgent = "business_requirements" | "architect" | "router";

/**
 * In an Assistant conversation every event after "run" carries `step` (the agent answering that
 * part of the turn, or the router) and `agent` (who produced it: inside an Architect step a
 * consultation or a Validator round has its own). Single-agent conversations leave `step` out.
 */
type Tagged = { step?: TurnAgent; agent?: TurnAgent | "validator" };

type DoneEvent = {
  status: "completed" | "stopped" | "failed";
  detail: string | null;
  message_id: string | null;
  run: { id: string; iterations?: number; tool_calls?: number; input_tokens?: number; output_tokens?: number };
  candidate?: { id: string; status: string; applied_seq: number | null; ready_for_review: boolean; blockers: number };
};

export type Route = "requirements" | "architect" | "requirements_then_architect" | "clarify";

export type AgentEvent = (
  | { type: "run"; run_id: string; redacted_credentials?: boolean }
  | { type: "route"; route: Route; reason: string; source: "router" | "user"; agents: TurnAgent[] }
  | { type: "handoff"; from: TurnAgent; to: TurnAgent; note: string }
  | ({ type: "step_done" } & DoneEvent)
  | { type: "text"; text: string }
  | {
      type: "tool";
      status: "started" | "ok" | "error";
      name: string;
      summary?: string;
      /** action: what happened to the record (added, answered, deferred, resolved, updated). */
      record?: { id: string; code: string; type: string; action?: string } | null;
    }
  | { type: "candidate"; candidate_id: string; status: string }
  | {
      type: "review";
      status: "started" | "completed" | "incomplete" | "failed";
      round: number;
      counts?: { passed: number; failed: number; unverified: number };
      run_id?: string;
    }
  | ({
      type: "done";
      /** Set when a chained turn stopped after its first agent: the router's note, saved as a reply. */
      chain_stopped?: string;
      note_message_id?: string;
    } & DoneEvent)
) & Tagged;

export async function sendAgentMessage(
  path: { tenant_id: string; project_id: string },
  conversationId: string,
  /** `agent` sends the message straight to one agent of an Assistant conversation, skipping the router. */
  body: {
    text: string;
    attachment_version_ids: string[];
    agent?: "business_requirements" | "architect";
    /** With `agent`: the earlier turn's run whose route this overrides ("send to the other agent instead"). */
    instead_of_run_id?: string;
  },
  onEvent: (e: AgentEvent) => void,
): Promise<void> {
  const resp = await fetch(
    `/api/tenants/${path.tenant_id}/projects/${path.project_id}/conversations/${conversationId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...(await authHeaders()) },
      body: JSON.stringify(body),
    },
  );
  if (!resp.ok || !resp.body) {
    let detail: unknown = {};
    try {
      detail = await resp.json();
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, detail as ApiError["body"]);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as AgentEvent);
      }
    }
  }
}

/** Upload a file as a new project document (multipart; not expressible in the typed client). */
export async function uploadDocument(
  path: { tenant_id: string; project_id: string },
  file: File,
): Promise<{ id: string; title: string; latest: { id: string; status: string; version_no: number; filename: string; notes: string[]; status_detail: string | null } }> {
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch(`/api/tenants/${path.tenant_id}/projects/${path.project_id}/documents`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(resp.status, data);
  return data;
}
