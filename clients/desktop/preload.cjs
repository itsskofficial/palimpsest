/**
 * The only surface the capture window gets.
 *
 * Context isolation is on, so the renderer has no Node and no `require`. Everything it
 * can do is listed here, which keeps the review UI — which loads a local HTTP page —
 * from being able to reach the filesystem if it were ever compromised.
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
  openReview: () => ipcRenderer.invoke("open-review"),
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

  readEnv: () => ipcRenderer.invoke("read-env"),
  saveEnv: (values) => ipcRenderer.invoke("save-env", values),

  on: (channel, handler) => {
    const allowed = ["focus-input", "status", "log", "show-settings"];
    if (!allowed.includes(channel)) return;
    ipcRenderer.on(channel, (_event, payload) => handler(payload));
  },
});
