/**
 * The capture window.
 *
 * One rule shapes all of it: submitting must be instant. The window queues the capture
 * and hides itself; it never waits for ingestion, because ingestion takes tens of
 * seconds and the whole point of a global shortcut is that it does not interrupt what
 * you were doing.
 */

const $ = (id) => document.getElementById(id);
const api = window.palimpsest;

let serverUrl = null;

// ---------------------------------------------------------------------------
// what a pasted string actually is
// ---------------------------------------------------------------------------

/**
 * Decide how to send what is in the box.
 *
 * A bare URL is sent as a `spec` so the server's adapters take over — YouTube fetches
 * captions, a PDF link is parsed as a PDF. Anything else is text. Guessing wrong in the
 * URL direction is harmless; guessing wrong the other way turns a link into a note whose
 * entire content is the link, which is silently useless.
 */
function classify(body, asTranscript) {
  const trimmed = body.trim();
  if (asTranscript) return { text: trimmed, kind: "transcript" };

  const singleLine = !/\s/.test(trimmed);
  if (singleLine && /^https?:\/\/\S+$/i.test(trimmed)) return { spec: trimmed };
  // A Windows or POSIX path typed or pasted in.
  if (singleLine && /^([a-zA-Z]:[\\/]|\/|\\\\)/.test(trimmed)) return { spec: trimmed };
  return { text: trimmed };
}

function say(message, kind = "ok", target = "result") {
  const box = $(target);
  box.textContent = message;
  box.className = `result result--${kind}`;
  box.hidden = false;
}

// ---------------------------------------------------------------------------
// sending
// ---------------------------------------------------------------------------

async function send() {
  const body = $("input").value.trim();
  if (!body) return;
  if (!serverUrl) return say("The server is not running yet.", "err");

  const payload = { ...classify(body, $("as-transcript").checked), origin: "desktop" };
  $("send").disabled = true;
  try {
    const response = await fetch(`${serverUrl}/v1/jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail.slice(0, 240));
    }
    $("input").value = "";
    $("as-transcript").checked = false;
    // Hide immediately. The job outlives this window, which is the entire reason the
    // server has a queue rather than a synchronous ingest endpoint.
    api.hide();
  } catch (error) {
    say(error.message, "err");
  } finally {
    $("send").disabled = false;
  }
}

async function sendPaths(paths) {
  if (!paths.length) return;
  say(`Queueing ${paths.length} file${paths.length > 1 ? "s" : ""}…`);
  const results = await api.capturePaths(paths);
  const failed = results.filter((r) => !r.ok);
  if (failed.length) {
    say(`${results.length - failed.length} queued, ${failed.length} failed:\n` +
        failed.map((f) => `${f.path}: ${f.error}`).join("\n"), "err");
  } else {
    api.hide();
  }
}

// ---------------------------------------------------------------------------
// events
// ---------------------------------------------------------------------------

$("send").addEventListener("click", send);
$("review").addEventListener("click", () => api.openReview());
$("browse").addEventListener("click", async () => sendPaths(await api.pickFiles()));

$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") api.hide();
});

// -- drag and drop over the whole window ------------------------------------

let dragDepth = 0;
window.addEventListener("dragenter", (event) => {
  event.preventDefault();
  dragDepth += 1;
  $("drop-veil").hidden = false;
});
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("dragleave", () => {
  // `dragleave` fires for every child element the cursor crosses, so a plain
  // hide-on-leave flickers the veil off while the file is still over the window.
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) $("drop-veil").hidden = true;
});
window.addEventListener("drop", async (event) => {
  event.preventDefault();
  dragDepth = 0;
  $("drop-veil").hidden = true;

  const paths = [...event.dataTransfer.files]
    .map((file) => api.pathForFile(file))
    .filter(Boolean);
  if (paths.length) return sendPaths(paths);

  // Dragging a link or selected text out of a browser gives no files, just text.
  const text = event.dataTransfer.getData("text/uri-list") ||
               event.dataTransfer.getData("text/plain");
  if (text) {
    $("input").value = text.trim();
    $("input").focus();
  }
});

// -- settings ---------------------------------------------------------------

const FIELDS = [
  "ANTHROPIC_API_KEY", "NOTION_TOKEN", "PALIMPSEST_NOTION_ROOTS",
  "PALIMPSEST_APPLY", "PALIMPSEST_AUTONOMY",
  "DEEPGRAM_API_KEY", "GROQ_API_KEY", "SARVAM_API_KEY",
  "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHATS",
  "FIRECRAWL_API_KEY",
];

function showView(which) {
  $("view-capture").hidden = which !== "capture";
  $("view-settings").hidden = which !== "settings";
}

$("settings").addEventListener("click", async () => {
  const env = await api.readEnv();
  for (const key of FIELDS) $(key).value = env[key] ?? "";
  showView("settings");
});

$("settings-back").addEventListener("click", () => showView("capture"));

$("settings-save").addEventListener("click", async () => {
  const values = Object.fromEntries(FIELDS.map((key) => [key, $(key).value.trim()]));
  const { restartNeeded } = await api.saveEnv(values);
  say(
    restartNeeded
      ? "Saved. Quit palimpsest from the tray and reopen it for the keys to take effect."
      : "Saved.",
    "ok",
    "settings-note",
  );
});

// -- from the main process --------------------------------------------------

api.on("focus-input", () => {
  showView("capture");
  $("input").focus();
  $("input").select();
});

api.on("status", (step) => {
  const el = $("status");
  el.textContent = step;
  el.className = `status${step === "ready" ? " status--ready" : ""}`;
});

api.on("show-settings", (env) => {
  for (const key of FIELDS) $(key).value = env[key] ?? "";
  showView("settings");
});

api.serverUrl().then((url) => (serverUrl = url));
// The URL is only known once the backend is up, so re-ask until it is.
const poll = setInterval(async () => {
  if (serverUrl) return clearInterval(poll);
  serverUrl = await api.serverUrl();
}, 1000);
