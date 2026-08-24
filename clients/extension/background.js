/**
 * The service worker: context menus, and the notifications that follow a capture.
 *
 * MV3 service workers are killed aggressively when idle, so nothing here holds state
 * between events. That is fine, because the durable state lives in the server's job
 * table — which is the same reason the popup can be closed the instant you click it.
 */

import { capture } from "./api.js";
import { captureTab } from "./router.js";

const MENUS = [
  { id: "palimpsest-page", title: "Capture this page", contexts: ["page"] },
  { id: "palimpsest-selection", title: "Capture selection", contexts: ["selection"] },
  { id: "palimpsest-transcript", title: "Capture transcript", contexts: ["page"] },
  { id: "palimpsest-link", title: "Capture this link", contexts: ["link"] },
  { id: "palimpsest-image", title: "Capture this image", contexts: ["image"] },
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    for (const menu of MENUS) chrome.contextMenus.create(menu);
  });
});

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/128.png",
    title,
    message: message.slice(0, 300),
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === "palimpsest-link" && info.linkUrl) {
      await capture({ spec: info.linkUrl, title: info.selectionText || undefined });
      notify("Queued", info.linkUrl);
      return;
    }
    if (info.menuItemId === "palimpsest-image" && info.srcUrl) {
      await capture({ spec: info.srcUrl, title: tab?.title });
      notify("Queued", info.srcUrl);
      return;
    }

    const mode = {
      "palimpsest-page": "auto",
      "palimpsest-selection": "selection",
      "palimpsest-transcript": "transcript",
    }[info.menuItemId];
    if (!mode) return;

    const { how } = await captureTab(tab, mode);
    notify("Queued", `${tab.title} — ${how}`);
  } catch (error) {
    // A failed capture must be loud. The whole promise of a capture tool is that the
    // thing you sent it is safe; a silent failure breaks that in the worst way,
    // because you find out weeks later that it was never there.
    notify("Capture failed", error.message);
  }
});
