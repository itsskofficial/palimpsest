import { saveSettings, settings, status } from "./api.js";

const $ = (id) => document.getElementById(id);

async function load() {
  const values = await settings();
  $("server").value = values.server;
  $("apiKey").value = values.apiKey;
  $("reviewer").value = values.reviewer;
}

$("save").addEventListener("click", async () => {
  await saveSettings({
    server: $("server").value.trim().replace(/\/+$/, ""),
    apiKey: $("apiKey").value.trim(),
    reviewer: $("reviewer").value.trim(),
  });

  const box = $("message");
  box.hidden = false;
  // Saving and then immediately proving the settings work is worth the extra call:
  // the alternative is discovering a typo the next time you try to capture something.
  try {
    const info = await status();
    box.className = "message message--ok";
    box.textContent = `Saved. Connected to palimpsest ${info.version}.`;
  } catch (error) {
    box.className = "message message--err";
    box.textContent = `Saved, but could not connect: ${error.message}`;
  }
});

load();
