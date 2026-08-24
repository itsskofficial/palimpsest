/**
 * The desktop app: a keystroke, a box, and somewhere for anything to land.
 *
 * The whole design goal is that capturing costs nothing. Press the shortcut anywhere,
 * drop a file or paste a link, press Enter, carry on — the window is gone before the
 * ingest has started, because the server queues the work and outlives the window.
 *
 * The capture window is deliberately *hidden* rather than destroyed when dismissed.
 * Recreating a BrowserWindow takes a few hundred milliseconds, which is exactly long
 * enough to feel like the shortcut did not fire, and a capture tool that feels
 * unreliable is one you stop trusting with the only copy of a thought.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  app, BrowserWindow, clipboard, dialog, globalShortcut, ipcMain, Menu,
  nativeImage, shell, Tray,
} from "electron";

import { Backend, findPython } from "./backend.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");
const SHORTCUT = "CommandOrControl+Shift+Space";

let tray = null;
let captureWindow = null;
let reviewWindow = null;
let backend = null;
const logLines = [];

// ---------------------------------------------------------------------------
// configuration
// ---------------------------------------------------------------------------

const envPath = () => join(app.getPath("userData"), "palimpsest.env");

/** Read `KEY=value` lines. The app's keys live beside its data, not in the repo. */
function readEnv() {
  const path = envPath();
  if (!existsSync(path)) {
    // Fall back to a .env in the checkout, so someone who already configured the CLI
    // does not have to type their keys a second time.
    const fallback = join(REPO_ROOT, ".env");
    if (!existsSync(fallback)) return {};
    return parseEnv(readFileSync(fallback, "utf8"));
  }
  return parseEnv(readFileSync(path, "utf8"));
}

function parseEnv(text) {
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    out[trimmed.slice(0, eq).trim()] = trimmed
      .slice(eq + 1)
      .trim()
      .replace(/^["']|["']$/g, "");
  }
  return out;
}

function writeEnv(values) {
  const body = Object.entries(values)
    .filter(([, v]) => v !== "" && v != null)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
  writeFileSync(envPath(), `${body}\n`, { mode: 0o600 });
}

function log(line, level = "info") {
  const entry = `${new Date().toISOString().slice(11, 19)} ${line}`;
  logLines.push(entry);
  if (logLines.length > 500) logLines.shift();
  if (level === "error") console.error(entry);
  else console.log(entry);
  captureWindow?.webContents.send("log", entry);
}

// ---------------------------------------------------------------------------
// windows
// ---------------------------------------------------------------------------

function icon() {
  const path = join(HERE, "icon.png");
  return existsSync(path) ? nativeImage.createFromPath(path) : nativeImage.createEmpty();
}

function makeCaptureWindow() {
  const win = new BrowserWindow({
    width: 560,
    height: 380,
    show: false,
    frame: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    transparent: false,
    backgroundColor: "#00000000",
    webPreferences: { preload: join(HERE, "preload.cjs") },
  });
  win.loadFile(join(HERE, "capture.html"));
  // Dismiss on losing focus: the window is modal in spirit, and having it linger
  // behind other windows is how you end up with three of them.
  win.on("blur", () => win.hide());
  win.on("close", (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      win.hide();
    }
  });
  return win;
}

function showCapture() {
  if (!captureWindow) captureWindow = makeCaptureWindow();
  captureWindow.center();
  captureWindow.show();
  captureWindow.focus();
  captureWindow.webContents.send("focus-input", {
    clipboard: clipboard.readText().slice(0, 20000),
  });
}

function showReview() {
  if (!backend) return;
  if (reviewWindow && !reviewWindow.isDestroyed()) {
    reviewWindow.show();
    reviewWindow.focus();
    return;
  }
  reviewWindow = new BrowserWindow({
    width: 1100,
    height: 800,
    title: "palimpsest — review",
    icon: icon(),
  });
  reviewWindow.loadURL(backend.url);
  reviewWindow.on("closed", () => (reviewWindow = null));
}

// ---------------------------------------------------------------------------
// tray
// ---------------------------------------------------------------------------

function buildTray() {
  tray = new Tray(icon());
  tray.setToolTip("palimpsest");
  refreshTray("starting…");
  tray.on("click", showCapture);
}

function refreshTray(status) {
  if (!tray) return;
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: `palimpsest — ${status}`, enabled: false },
      { type: "separator" },
      { label: `Capture (${SHORTCUT.replace("CommandOrControl", "Ctrl")})`, click: showCapture },
      { label: "Review patches", click: showReview },
      {
        label: "Organise the workspace",
        click: () => {
          showReview();
          // The review UI has the organise view; deep-linking there saves a click on
          // the action most likely to be why someone opened the window at all.
          reviewWindow?.webContents.once("did-finish-load", () =>
            reviewWindow.webContents.executeJavaScript(
              "location.hash = '#organise'",
            ).catch(() => {}),
          );
        },
      },
      { type: "separator" },
      { label: "Settings…", click: showSettings },
      { label: "Open the log", click: showLog },
      {
        label: "Start with Windows",
        type: "checkbox",
        checked: app.getLoginItemSettings().openAtLogin,
        click: (item) =>
          app.setLoginItemSettings({ openAtLogin: item.checked, args: ["--hidden"] }),
      },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          app.isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
}

function showSettings() {
  if (!captureWindow) captureWindow = makeCaptureWindow();
  captureWindow.show();
  captureWindow.focus();
  captureWindow.webContents.send("show-settings", readEnv());
}

function showLog() {
  const win = new BrowserWindow({ width: 900, height: 600, title: "palimpsest — log" });
  const body = logLines.join("\n").replace(/[<&]/g, (c) => (c === "<" ? "&lt;" : "&amp;"));
  win.loadURL(
    "data:text/html;charset=utf-8," +
      encodeURIComponent(
        `<body style="margin:0;background:#1c1b1a;color:#ddd;font:12px ui-monospace,Consolas,monospace">
         <pre style="padding:14px;white-space:pre-wrap">${body}</pre></body>`,
      ),
  );
}

// ---------------------------------------------------------------------------
// IPC — everything the capture window is allowed to ask for
// ---------------------------------------------------------------------------

ipcMain.handle("server-url", () => backend?.url ?? null);
ipcMain.handle("hide", () => captureWindow?.hide());
ipcMain.handle("open-review", () => showReview());
ipcMain.handle("read-env", () => readEnv());

ipcMain.handle("save-env", async (_event, values) => {
  writeEnv(values);
  // Keys are read by the server process at startup, so they only take effect after a
  // restart. Saying so beats silently doing nothing until the next reboot.
  return { restartNeeded: true };
});

ipcMain.handle("pick-files", async () => {
  const { canceled, filePaths } = await dialog.showOpenDialog({
    properties: ["openFile", "multiSelections"],
    filters: [
      { name: "Anything readable", extensions: ["pdf", "md", "txt", "csv", "xlsx", "xls", "png", "jpg", "jpeg", "webp", "vtt", "srt"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  return canceled ? [] : filePaths;
});

/**
 * Queue local files by path rather than by upload.
 *
 * A browser extension has to send bytes because it cannot see the filesystem. This app
 * can, and the server runs on the same machine, so handing over a path avoids copying
 * a 60 MB PDF through an HTTP request to a process that could simply open it.
 */
ipcMain.handle("capture-paths", async (_event, paths) => {
  const results = [];
  for (const path of paths) {
    const response = await fetch(`${backend.url}/v1/jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec: path, origin: "desktop" }),
    });
    results.push(
      response.ok
        ? { ok: true, path, job: await response.json() }
        : { ok: false, path, error: (await response.text()).slice(0, 300) },
    );
  }
  return results;
});

ipcMain.handle("open-external", (_event, url) => shell.openExternal(url));

// ---------------------------------------------------------------------------
// lifecycle
// ---------------------------------------------------------------------------

// A second instance should surface the first one's window, not start a second server
// against the same SQLite file.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showCapture);

  app.whenReady().then(async () => {
    buildTray();
    captureWindow = makeCaptureWindow();

    if (!globalShortcut.register(SHORTCUT, showCapture)) {
      log(`could not register ${SHORTCUT}; another app already owns it`, "error");
    }

    backend = new Backend({
      venvDir: join(app.getPath("userData"), "venv"),
      repoRoot: REPO_ROOT,
      port: Number(process.env.PALIMPSEST_PORT || 8100),
      env: readEnv(),
      log,
    });

    try {
      const { adopted, url } = await backend.start((step) => {
        log(step);
        refreshTray(step);
        captureWindow?.webContents.send("status", step);
      });
      refreshTray(adopted ? "attached" : "running");
      log(`ready at ${url}`);
      captureWindow?.webContents.send("status", "ready");
    } catch (error) {
      log(error.message, "error");
      refreshTray("failed");
      const python = findPython();
      dialog.showErrorBox(
        "palimpsest could not start",
        `${error.message}\n\n${python ? `Python ${python.version} was found.` : "No suitable Python was found."}`,
      );
    }

    if (!process.argv.includes("--hidden")) showCapture();
  });

  app.on("window-all-closed", (event) => event.preventDefault()); // tray app
  app.on("before-quit", () => {
    app.isQuitting = true;
    globalShortcut.unregisterAll();
    backend?.stop();
  });
}
