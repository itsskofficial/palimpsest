/**
 * The popup: say what will happen, do it in one click, then get out of the way.
 *
 * The popup deliberately does not wait for ingestion. It queues the capture, shows the
 * job id, and lets you close the window — a capture surface that holds you hostage for
 * two minutes is one you stop using, and the queue exists precisely so it does not
 * have to.
 */

import { capture, jobs, settings, status } from "./api.js";
import { captureTab, isGated, isYouTube } from "./router.js";

const $ = (id) => document.getElementById(id);
let currentTab = null;

function say(text, kind = "ok") {
  const box = $("message");
  box.textContent = text;
  box.className = `message message--${kind}`;
  box.hidden = false;
}

function plan(url, selectionHint) {
  if (selectionHint) return "Selected text will be captured as a note.";
  if (isYouTube(url)) return "YouTube — the server fetches the captions and cites timestamps.";
  if (isGated(url)) return "Gated site — the transcript will be read from this page.";
  return "The page will be fetched and read on the server.";
}

async function paintHealth() {
  try {
    const info = await status();
    const problems = info.problems || [];
    const pill = $("health");
    if (problems.length) {
      pill.textContent = `${problems.length} to fix`;
      pill.className = "pill pill--warn";
      pill.title = problems.join("\n");
    } else {
      pill.textContent = info.config?.apply ? "writes on" : "propose only";
      pill.className = "pill pill--ok";
      pill.title = `palimpsest ${info.version}`;
    }
    $("capture").disabled = false;
  } catch (error) {
    const pill = $("health");
    pill.textContent = "offline";
    pill.className = "pill pill--err";
    pill.title = error.message;
    say(error.message, "err");
  }
}

async function paintJobs() {
  try {
    const { jobs: rows } = await jobs(6);
    $("jobs").innerHTML = "";
    for (const row of rows) {
      const li = document.createElement("li");
      const status_ = document.createElement("span");
      status_.className = `status status--${row.status}`;
      status_.textContent = row.status;
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = row.title || row.url || row.spec?.slice(0, 60) || row.job_id;
      if (row.error) what.title = row.error;
      li.append(status_, what);
      $("jobs").append(li);
    }
  } catch {
    /* the health pill already reports the server being unreachable */
  }
}

async function init() {
  const { server } = await settings();
  $("review-link").href = server;

  [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("tab-title").textContent = currentTab?.title || "…";
  $("tab-title").title = currentTab?.url || "";
  $("tab-plan").textContent = plan(currentTab?.url || "");

  await Promise.all([paintHealth(), paintJobs()]);
}

$("capture").addEventListener("click", async () => {
  const button = $("capture");
  button.disabled = true;
  button.textContent = "Queueing…";
  try {
    const { how } = await captureTab(currentTab, "auto");
    say(`Queued: ${how}`, "ok");
    button.textContent = "Queued ✓";
    await paintJobs();
  } catch (error) {
    say(error.message, "err");
    button.textContent = "Capture this page";
    button.disabled = false;
  }
});

$("paste-send").addEventListener("click", async () => {
  const body = $("paste-body").value.trim();
  if (!body) return say("Nothing to send.", "err");
  try {
    await capture({
      text: body,
      kind: $("paste-transcript").checked ? "transcript" : undefined,
      title: currentTab?.title,
      url: $("paste-transcript").checked ? currentTab?.url : undefined,
    });
    $("paste-body").value = "";
    say("Queued.", "ok");
    await paintJobs();
  } catch (error) {
    say(error.message, "err");
  }
});

$("options-link").addEventListener("click", (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

init();
