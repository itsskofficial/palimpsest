/**
 * Talking to the Python backend.
 *
 * The UI is a static export served *by* that backend (and loaded by Electron from the
 * same origin), so requests are same-origin relative paths in production. In `next dev`
 * the UI runs on :3000 and the backend on :8100, so the base is overridable — that split
 * is the only reason this file has a base URL at all.
 */

export const API_BASE =
  process.env.NODE_ENV === "development" ? "http://127.0.0.1:8100" : "";

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body && !(init.body instanceof FormData)
          ? { "Content-Type": "application/json" }
          : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (cause) {
    // A refused connection is the common failure and has nothing to do with the UI.
    throw new ApiError(
      "Can't reach palimpsest. Is the backend running?",
    );
  }
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail =
      (body as any)?.detail ?? (body as any)?.error ?? text ?? `HTTP ${res.status}`;
    throw new ApiError(
      typeof detail === "string" ? detail : JSON.stringify(detail),
      res.status,
    );
  }
  return body as T;
}

function safeJson(text: string) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

// -- types -------------------------------------------------------------------

export type SetupState = {
  configured: boolean;
  steps: {
    model: boolean;
    notion: boolean;
    root: boolean;
    telegram_token: boolean;
    telegram_paired: boolean;
  };
  optional: { transcription: string | null; tracing: boolean };
  problems: string[];
  apply: boolean;
  autonomy: string;
};

export type JobEvent = {
  job_id: string;
  status: "queued" | "running" | "done" | "failed";
  title: string;
  kind?: string | null;
  claims?: number | null;
  by_relation?: Record<string, number> | null;
  applied?: number | null;
  held?: number | null;
  approval_id?: string | null;
  error?: string | null;
  playlist?: {
    title: string;
    url: string;
    videos: number;
    total: number;
    skipped: number;
    truncated: boolean;
  } | null;
};

export type Operation = {
  op_id: string;
  kind: string;
  relation: string | null;
  risk: string;
  page: string;
  page_url?: string | null;
  text?: string | null;
  was?: string | null;
  why?: string | null;
  confidence?: number | null;
};

export type Approval = {
  approval_id: string;
  patch_id: string;
  summary?: string | null;
  status: string;
  requested_at: number;
  operations: Operation[];
  source?: { title?: string; kind?: string; url?: string | null };
};

// -- endpoints ---------------------------------------------------------------

export type LocalRuntime = {
  provider: string;
  base_url: string;
  models: string[];
  embedding_models: string[];
  suggested_model: string;
  suggested_embedding: string;
};

export type ActivityEntry = {
  patch_id: string;
  status: string;
  at: number | null;
  reviewer: string | null;
  source: { title?: string; kind?: string; url?: string } | null;
  operations: number;
  applied: number;
  undoable: boolean;
  reverted: boolean;
  relations: string[];
  pages: string[];
  /** What it could not decide on its own, and why. Usually a contradiction. */
  review?: {
    reason: string | null;
    relation: string | null;
    confidence: number | null;
    rationale: string | null;
    claim: string | null;
    existing_text: string | null;
    page: string | null;
  }[];
};

/** What `palimpsest demo` is offering, if this is a demo vault at all. */
export type DemoState = {
  demo: boolean;
  vault: string | null;
  prompts: { label: string; text: string; why: string }[];
};

export const api = {
  status: () => request<any>("/v1/status"),
  setupState: () => request<SetupState>("/v1/setup/state"),
  demo: () => request<DemoState>("/v1/demo"),

  validate: (provider: string, token: string) =>
    post<{
      ok: boolean;
      detail?: string;
      error?: string;
      username?: string;
      /** Notion only: whether the integration can actually see anything yet. */
      shared_pages?: number;
      warning?: string | null;
    }>("/v1/setup/validate", { provider, token }),

  notionPages: (token: string) =>
    request<{ pages: { page_id: string; title: string }[] }>(
      `/v1/setup/notion/pages?token=${encodeURIComponent(token)}`,
    ),

  createRoot: (token: string, title?: string) =>
    post<{ page_id: string; url: string }>("/v1/setup/notion/root", { token, title }),

  pairTelegram: (token: string) =>
    post<{ paired: boolean; chat_id?: number; name?: string }>(
      "/v1/setup/telegram/pair",
      { token },
    ),

  getSettings: () =>
    request<{ values: Record<string, string>; present: Record<string, boolean>; config_path: string }>(
      "/v1/settings",
    ),
  saveSettings: (values: Record<string, string>) =>
    post<{ saved: string[] }>("/v1/settings", { values }),

  jobs: (limit = 40) => request<{ jobs: any[] }>(`/v1/jobs?limit=${limit}`),
  capture: (payload: { spec?: string; text?: string; kind?: string; title?: string; url?: string }) =>
    post<{ job_id: string }>("/v1/jobs", { ...payload, origin: "ui" }),

  upload: async (files: File[]) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    form.append("origin", "ui");
    return request<{ jobs: any[]; count: number }>("/v1/ingest/upload", {
      method: "POST",
      body: form,
    });
  },

  approvals: (status = "pending") =>
    request<{ approvals: Approval[] }>(`/v1/approvals?status=${status}`),
  resolve: (id: string, decision: "approved" | "rejected") =>
    post<any>(`/v1/approvals/${id}/resolve`, { decision, by: "ui" }),

  agent: (message: string, sessionId = "ui") =>
    post<{ text: string; tools: string[]; approvals: string[]; error?: string }>(
      "/v1/agent",
      { message, session_id: sessionId },
    ),

  localRuntimes: () => request<{ runtimes: LocalRuntime[] }>("/v1/setup/local"),

  activity: (limit = 60) =>
    request<{ activity: ActivityEntry[] }>(`/v1/activity?limit=${limit}`),
  undo: (patchId: string) => post<{ ok: boolean }>(`/v1/patches/${patchId}/undo`),

  sweep: (kind: string) => post<any>(`/v1/sweep/${kind}`),
  organise: () => post<any>("/v1/organise", {}),
  sync: () => post<any>("/v1/sync", {}),
};

// -- the live stream ---------------------------------------------------------

/**
 * Subscribe to backend events. Returns an unsubscribe function.
 *
 * EventSource reconnects on its own, which is what we want for a desktop app whose
 * backend may restart underneath it.
 */
export function subscribe(
  handlers: { onJob?: (e: JobEvent) => void; onApproval?: (e: any) => void; onOpen?: () => void; onError?: () => void },
): () => void {
  if (typeof window === "undefined") return () => {};
  const es = new EventSource(`${API_BASE}/v1/events`);
  es.addEventListener("open", () => handlers.onOpen?.());
  es.addEventListener("job", (e) => handlers.onJob?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("approval", (e) =>
    handlers.onApproval?.(JSON.parse((e as MessageEvent).data)),
  );
  es.addEventListener("error", () => handlers.onError?.());
  return () => es.close();
}
