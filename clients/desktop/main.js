/**
 * The desktop app: two windows over one local service.
 *
 * The **main window** is the product — the same UI the Python service serves at
 * `palimpsest serve`, loaded over `http://127.0.0.1`. It is not reimplemented here.
 * Shipping the interface with the backend rather than with the shell means the browser,
 * the app and the onboarding wizard can never disagree about what a setting is called,
 * and it means this file stays small enough to reason about.
 *
 * The **capture window** is the global shortcut. Press it anywhere, drop a file or paste
 * a link, press Enter, carry on — the window is gone before the ingest has started,
 * because the server queues the work and outlives the window. It is deliberately *hidden*
 * rather than destroyed when dismissed: recreating a BrowserWindow takes a few hundred
 * milliseconds, which is exactly long enough to feel like the shortcut did not fire, and
 * a capture tool that feels unreliable is one you stop trusting with the only copy of a
 * thought.
 *
 * Configuration is not this process's business. Keys live in the Python service's own
 * config file and are edited through its Settings screen, so there is exactly one store
 * and no question about which copy wins.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  app, BrowserWindow, clipboard, dialog, globalShortcut, ipcMain, Menu,
  nativeImage, shell, Tray,
} from "electron";

import { Backend, findPython } from "./backend.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");
/**
 * Candidate capture shortcuts, best first.
 *
 * `Ctrl+Shift+Space` is the one to want and the one most likely to be taken -- on
 * Windows an IME or a launcher usually owns it, and `globalShortcut.register`
 * answers by returning false rather than by throwing. A single hard-coded binding
 * therefore lost the headline feature to whichever app booted first, silently, with
 * a line in a log nobody opens.
 *
 * So: try them in order, keep the first that takes, and put the winner in the tray
 * menu so the answer to "what is my shortcut" is where somebody would look.
 */
const SHORTCUTS = [
  "CommandOrControl+Shift+Space",
  "CommandOrControl+Alt+Space",
  "CommandOrControl+Shift+P",
  "CommandOrControl+Alt+P",
];

/** Whichever of the above actually registered, or null if every one was taken. */
let shortcut = null;

/** How a shortcut should be written for a person: Cmd on macOS, Ctrl elsewhere. */
function prettyShortcut(combo) {
  return combo.replace("CommandOrControl", process.platform === "darwin" ? "Cmd" : "Ctrl");
}

let tray = null;
let captureWindow = null;
let mainWindow = null;
let backend = null;
let status = "starting…";
const logLines = [];

// ---------------------------------------------------------------------------
// configuration
// ---------------------------------------------------------------------------

/**
 * The Python service's config file — the single store this app defers to.
 *
 * Mirrors `palimpsest.config.config_path()`. Duplicating the rule in two languages is
 * not lovely, but the alternative is starting a Python process to ask a question we need
 * answered before we have a Python process.
 */
function configPath() {
  if (process.platform === "win32") {
    return join(app.getPath("appData"), "palimpsest", "config.env");
  }
  const xdg = process.env.XDG_CONFIG_HOME || join(app.getPath("home"), ".config");
  return join(xdg, "palimpsest", "config.env");
}

/**
 * Move keys out of the app's old private store and into the shared one, once.
 *
 * Earlier versions kept their own `palimpsest.env` in the app data directory and passed
 * it to the server as environment variables. Real environment variables outrank the
 * config file, so leaving that in place would mean the Settings screen appeared to save
 * and then changed nothing — the worst possible failure for a settings screen. Moving
 * (not copying) the file makes the migration idempotent by construction.
 */
function migrateLegacyEnv() {
  const legacy = join(app.getPath("userData"), "palimpsest.env");
  if (!existsSync(legacy)) return;
  const target = configPath();
  if (existsSync(target)) {
    renameSync(legacy, `${legacy}.superseded`);
    log(`ignored the old ${legacy}; ${target} already exists`);
    return;
  }
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, readFileSync(legacy, "utf8"), { mode: 0o600 });
  renameSync(legacy, `${legacy}.migrated`);
  log(`moved saved keys to ${target}`);
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

/**
 * The window while the service is still coming up.
 *
 * A first run creates a virtualenv and downloads a few hundred megabytes of wheels, which
 * takes minutes. Loading `127.0.0.1` during that shows Chromium's connection-refused
 * page, and an app whose first screen is a browser error is an app you close. So the
 * window opens on a local page that says what is happening, and swaps to the real UI once
 * the server answers.
 */
function splash() {
  return (
    "data:text/html;charset=utf-8," +
    encodeURIComponent(`<!doctype html><meta charset="utf-8"><body style="margin:0;
      display:flex;align-items:center;justify-content:center;height:100vh;
      background:#f7f4ee;color:#5f574c;
      font:14px ui-sans-serif,'Segoe UI',system-ui,sans-serif">
      <div style="text-align:center">
        <div style="font:600 26px Georgia,serif;color:#241f1a">palimpsest</div>
        <div id="s" style="margin-top:14px;font-size:13px">${status}</div>
        <div style="margin-top:22px;font-size:11.5px;color:#8d8274;max-width:30rem">
          The first start installs the engine into its own virtualenv.
          It only happens once.
        </div>
      </div></body>`)
  );
}

function showMain(tab) {
  const hash = tab ? `#${tab}` : "";
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    if (backend && tab) {
      mainWindow.webContents
        .executeJavaScript(`location.hash = ${JSON.stringify(hash)}`)
        .catch(() => {});
    }
    return;
  }

  mainWindow = new BrowserWindow({
    width: 1080,
    height: 820,
    minWidth: 720,
    minHeight: 560,
    title: "palimpsest",
    icon: icon(),
    backgroundColor: "#f7f4ee",
    autoHideMenuBar: true,
    // No preload, no Node: this window renders a page whose content comes from whatever
    // you have captured, and there is nothing here it needs from the filesystem.
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });

  // Links to anywhere but the local server open in the real browser rather than turning
  // this window into an unlabelled, un-navigable one-tab browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (backend && url.startsWith(backend.url)) return;
    event.preventDefault();
    shell.openExternal(url);
  });

  mainWindow.on("closed", () => (mainWindow = null));
  mainWindow.loadURL(backend?.ready ? backend.url + hash : splash());
}

/** Swap the splash for the real thing, or tell the splash what is happening. */
function refreshMain() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (backend?.ready) {
    if (!mainWindow.webContents.getURL().startsWith("http")) {
      mainWindow.loadURL(backend.url);
    }
    return;
  }
  mainWindow.webContents
    .executeJavaScript(
      `(document.getElementById("s")||{}).textContent = ${JSON.stringify(status)}`,
    )
    .catch(() => {});
}

// ---------------------------------------------------------------------------
// tray
// ---------------------------------------------------------------------------

/**
 * The engine version, when it is older than this shell. `null` when they agree.
 *
 * This is not a nicety. The venv is built once, on first run, and was then never
 * touched again -- so somebody who installed in September was still running
 * September's engine months later, with none of the fixes, and nothing anywhere said
 * so. It surfaced in the worst possible way: an installer built from the current
 * source installed an engine nine months older, and since the *interface* ships
 * inside the Python package, the window rendered a UI that predated the design. It
 * looked exactly like a botched redesign and was in fact a stale dependency.
 */
let engineStale = null;

function checkEngine() {
  const engine = backend?.engineVersion?.();
  const shell = app.getVersion();
  if (!engine || engine === shell) {
    engineStale = null;
    return;
  }
  engineStale = engine;
  log(
    `the engine is ${engine} but this app is ${shell}. The interface ships inside ` +
      "the engine, so the window is showing that version. Tray \u2192 Update the engine.",
    "warn",
  );
  refreshTray(`engine ${engine} \u2014 update available`);
}

async function updateEngine() {
  if (!backend) return;
  try {
    refreshTray("updating the engine\u2026");
    await backend.upgrade((message) => refreshTray(message));
    // The server is running the old code until it is restarted, and restarting it
    // is the whole point of having updated.
    backend.stop();
    backend.stopping = false;
    await backend.start((message) => refreshTray(message));
    checkEngine();
    refreshTray("ready");
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
  } catch (error) {
    log(`could not update the engine: ${error.message}`, "error");
    refreshTray("engine update failed");
  }
}

function buildTray() {
  tray = new Tray(icon());
  tray.setToolTip("palimpsest");
  refreshTray("starting…");
  tray.on("click", showCapture);
}

function refreshTray(next) {
  status = next;
  refreshMain();
  if (!tray) return;
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: `palimpsest — ${status}`, enabled: false },
      { type: "separator" },
      { label: "Open palimpsest", click: () => showMain() },
      {
        label: shortcut
          ? `Quick capture (${prettyShortcut(shortcut)})`
          : "Quick capture (no shortcut available)",
        click: showCapture,
      },
      { label: "Ask your notes", click: () => showMain("ask") },
      { type: "separator" },
      { label: "Settings…", click: () => showMain("settings") },
      { label: "Open the log", click: showLog },
      {
        label: engineStale
          ? `Update the engine (${engineStale} \u2192 ${app.getVersion()})\u2026`
          : "Check for engine updates\u2026",
        click: updateEngine,
      },
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

ipcMain.handle("server-url", () => (backend?.ready ? backend.url : null));
ipcMain.handle("hide", () => captureWindow?.hide());
ipcMain.handle("open-main", (_event, tab) => {
  captureWindow?.hide();
  showMain(typeof tab === "string" ? tab : undefined);
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
  app.on("second-instance", () => showMain());

  app.whenReady().then(async () => {
    migrateLegacyEnv();
    captureWindow = makeCaptureWindow();

    // Registering is instantaneous, so the tray is built once, afterwards, with the
    // shortcut that actually took rather than the one we hoped for.
    shortcut = SHORTCUTS.find((combo) => globalShortcut.register(combo, showCapture))
      ?? null;
    if (shortcut) {
      log(`quick capture: ${prettyShortcut(shortcut)}`);
    } else {
      log(
        `every capture shortcut is taken (${SHORTCUTS.map(prettyShortcut).join(", ")}). ` +
          "Quick capture is still on the tray menu.",
        "error",
      );
    }
    buildTray();

    backend = new Backend({
      venvDir: join(app.getPath("userData"), "venv"),
      repoRoot: REPO_ROOT,
      port: Number(process.env.PALIMPSEST_PORT || 8100),
      // The database and archive default to paths relative to the server's directory.
      // See `Backend.serverOptions` for what went wrong when this was left to chance.
      dataDir: app.getPath("userData"),
      env: { PALIMPSEST_CONFIG: configPath() },
      log,
    });

    // Open the window before starting the backend, not after: on a first run the install
    // takes minutes, and an app that shows nothing at all for that long is one you
    // assume failed and kill.
    if (!process.argv.includes("--hidden")) showMain();

    try {
      const { adopted, url } = await backend.start((step) => {
        log(step);
        refreshTray(step);
        captureWindow?.webContents.send("status", step);
      });
      refreshTray(adopted ? "attached" : "running");
      log(`ready at ${url}`);
      captureWindow?.webContents.send("status", "ready");
      refreshMain();
      // Only once the server is up, so the version read is of the thing now running.
      // Skipped for an adopted server: that one belongs to somebody's terminal and its
      // version is their business, not this app's.
      if (!adopted) checkEngine();
    } catch (error) {
      log(error.message, "error");
      refreshTray("failed");
      const python = findPython();
      dialog.showErrorBox(
        "palimpsest could not start",
        `${error.message}\n\n${python ? `Python ${python.version} was found.` : "No suitable Python was found."}`,
      );
    }
  });

  app.on("window-all-closed", (event) => event.preventDefault()); // tray app
  app.on("activate", () => showMain()); // macOS: the dock icon should bring it back
  app.on("before-quit", () => {
    app.isQuitting = true;
    globalShortcut.unregisterAll();
    backend?.stop();
  });
}
