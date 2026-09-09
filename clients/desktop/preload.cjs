/**
 * The only surface the capture window gets.
 *
 * Context isolation is on, so the renderer has no Node and no `require`. Everything it
 * can do is listed here. This preload is loaded *only* into the capture window, never
 * into the main window: the main window renders a local HTTP page whose content is
 * ultimately shaped by whatever you captured, so it gets no bridge at all.
 *
 * There is deliberately nothing here for reading or writing keys. Configuration lives in
 * one place — the config file the Python service owns, edited through its own Settings
 * screen — because a second store in the app's data directory would silently win or
 * silently lose depending on which one was written last.
 *
 * `webUtils.getPathForFile` is the reason this file is not just IPC plumbing: Electron
 * removed `File.path` in v32, so a dropped file's real path can only be recovered in a
 * preload. Without it, dropping a 60 MB PDF would mean reading it into the renderer and
 * posting the bytes to a server running on the same machine that could have just opened
 * the file.
 */

const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("palimpsest", {
  serverUrl: () => ipcRenderer.invoke("server-url"),
  hide: () => ipcRenderer.invoke("hide"),
  openMain: (tab) => ipcRenderer.invoke("open-main", tab),
  openExternal: (url) => ipcRenderer.invoke("open-external", url),

  pickFiles: () => ipcRenderer.invoke("pick-files"),
  capturePaths: (paths) => ipcRenderer.invoke("capture-paths", paths),
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return null;
    }
  },

  on: (channel, handler) => {
    const allowed = ["focus-input", "status", "log"];
    if (!allowed.includes(channel)) return;
    ipcRenderer.on(channel, (_event, payload) => handler(payload));
  },
});
