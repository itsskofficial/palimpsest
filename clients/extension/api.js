/**
 * Talking to the local palimpsest server.
 *
 * Everything goes to `POST /v1/jobs`, never to `/v1/ingest`. The difference matters
 * here more than anywhere else: `/v1/ingest` runs the whole pipeline before it
 * responds, and a popup is destroyed the moment you click away from it. The request
 * would be cancelled mid-extraction and the capture silently lost. The queue endpoint
 * returns an id in milliseconds and the work outlives the window that asked for it.
 */

const DEFAULTS = {
  server: "http://127.0.0.1:8100",
  apiKey: "",
  reviewer: "",
};

export async function settings() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  return { ...DEFAULTS, ...stored, server: stored.server.replace(/\/+$/, "") };
}

export async function saveSettings(values) {
  await chrome.storage.sync.set(values);
}

async function request(path, options = {}) {
  const { server, apiKey } = await settings();
  const headers = { ...(options.headers || {}) };
  // The server only requires this when PALIMPSEST_API_KEY is set; sending it always is
  // harmless and means a user who turns auth on later does not have to re-configure.
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  let response;
  try {
    response = await fetch(`${server}${path}`, { ...options, headers });
  } catch (cause) {
    // A refused connection is by far the most common failure and it has nothing to do
    // with the extension, so say what it actually is rather than "failed to fetch".
    throw new Error(
      `Could not reach palimpsest at ${server}. Is it running? Start it with ` +
        `\`palimpsest serve\`, or open the desktop app.`,
      { cause },
    );
  }

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload?.detail || text || `HTTP ${response.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

/** Queue one capture. Returns the job row. */
export function capture({ spec, text, kind, title, url, origin = "extension" }) {
  return request("/v1/jobs", {
    method: "POST",
    body: JSON.stringify({ spec, text, kind, title, url, origin }),
  });
}

export function jobs(limit = 8) {
  return request(`/v1/jobs?limit=${limit}`);
}

export function status() {
  return request("/v1/status");
}

/** Upload files the page gave us — an image the user right-clicked, say. */
export async function upload(files, { title, url } = {}) {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  form.append("origin", "extension");
  if (title) form.append("title", title);
  if (url) form.append("url", url);
  return request("/v1/ingest/upload", { method: "POST", body: form });
}
