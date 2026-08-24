/**
 * Deciding what "capture this" means for the tab you are on.
 *
 * The rule is: send the *least processed* thing that still carries anchors.
 *
 * - **YouTube** — send the bare link. The server fetches the caption track itself and
 *   anchors every claim to a second of video. Scraping the page here would produce a
 *   worse transcript than the one the server can fetch for free.
 * - **Udemy, Coursera and anything else gated** — the server cannot log in, so the
 *   transcript has to come from the page. It is sent as `kind: "transcript"` with the
 *   lecture URL and title attached, which is what lets a claim cite `1:42:07` instead
 *   of a character offset.
 * - **Everything else** — send the URL and let the web adapter fetch it. Firecrawl when
 *   configured, a stdlib reader when not.
 *
 * The distinction that matters is the middle one. Sending a gated lecture's URL looks
 * like it works and produces a source consisting of the login page.
 */

import { capture } from "./api.js";
import { pageContext, scrapeTranscript } from "./scrape.js";

/** Hosts whose content is behind a login, so the page must supply the text. */
const GATED = [
  "udemy.com",
  "coursera.org",
  "edx.org",
  "pluralsight.com",
  "linkedin.com/learning",
  "skillshare.com",
  "datacamp.com",
  "educative.io",
  "frontendmasters.com",
  "oreilly.com",
  "maven.com",
];

const isYouTube = (url) => /(?:youtube\.com\/(?:watch|shorts)|youtu\.be\/)/.test(url);
const isGated = (url) => GATED.some((host) => url.includes(host));

async function inject(tabId, func) {
  const [result] = await chrome.scripting.executeScript({ target: { tabId }, func });
  return result?.result;
}

/**
 * Capture a tab. `mode` is "auto", "transcript", "link" or "selection".
 * Returns { job, how } so the caller can say what it did.
 */
export async function captureTab(tab, mode = "auto") {
  if (!tab?.id || !tab.url || /^(chrome|edge|about|chrome-extension):/.test(tab.url)) {
    throw new Error("There is nothing to capture on this tab.");
  }

  const context = (await inject(tab.id, pageContext)) || {
    url: tab.url,
    title: tab.title || tab.url,
    selection: "",
  };

  if (mode === "selection" || (mode === "auto" && context.selection.length > 40)) {
    if (!context.selection) throw new Error("Nothing is selected on this page.");
    const job = await capture({
      text: context.selection,
      title: context.title,
      url: context.url,
    });
    return { job, how: `selection (${context.selection.length} chars)` };
  }

  if (mode === "link" || (mode === "auto" && isYouTube(tab.url))) {
    const job = await capture({ spec: context.url, title: context.title });
    return { job, how: isYouTube(tab.url) ? "YouTube link — the server will fetch the captions" : "link" };
  }

  if (mode === "transcript" || (mode === "auto" && isGated(tab.url))) {
    const found = await inject(tab.id, scrapeTranscript);
    if (!found?.ok) {
      throw new Error(found?.note || "No transcript found on this page.");
    }
    const job = await capture({
      text: found.text,
      kind: "transcript",
      title: context.title,
      url: context.url,
    });
    return { job, how: `transcript, ${found.cues} cues (${found.method})` };
  }

  const job = await capture({ spec: context.url, title: context.title });
  return { job, how: "page" };
}

export { isGated, isYouTube };
