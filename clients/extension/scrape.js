/**
 * Pulling a transcript off a page you are logged into.
 *
 * Udemy and Coursera will not let a fetcher near a lecture, but your browser is already
 * authenticated and the transcript panel is sitting in the DOM. So the extension reads
 * it from the page you are looking at.
 *
 * **The deliberate decision here is to do almost no parsing.** The obvious approach is
 * to write a selector per platform and pull out cue text and timestamps as structured
 * data. That breaks every time either site ships a redesign, and it breaks silently —
 * you get an empty capture and no reason. Instead this returns the transcript region's
 * raw `innerText`, and the Python adapter turns it into timestamped cues. That parser
 * handles stacked, inline, bracketed, WebVTT and SRT shapes and is covered by tests, so
 * the fragile half lives where it can be verified and the browser half stays trivial.
 *
 * Finding the region is therefore the only real job, and it is done by timestamp
 * density rather than by class name: locate the elements whose own text begins with a
 * timestamp, walk up to their common ancestor, take its text. That works on both
 * platforms, survives their redesigns, and usually works on sites nobody thought about.
 *
 * Everything below must be self-contained. `chrome.scripting.executeScript` serialises
 * the function into the page, so a reference to anything outside it arrives as
 * `undefined` at runtime.
 */

/** Injected into the page. Returns { ok, text, method, cues, note }. */
export function scrapeTranscript() {
  const STAMP = /^\s*[[(]?\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\b/;

  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  };

  const clean = (raw) =>
    raw
      .split("\n")
      .map((line) => line.replace(/ /g, " ").trimEnd())
      .filter((line, i, all) => line.trim() || (all[i - 1] || "").trim())
      .join("\n")
      .trim();

  // -- 1. the fast path: containers these platforms have kept stable -----------
  const KNOWN = [
    '[data-purpose="transcript-panel"]',
    '[data-purpose="transcript-cue-container"]',
    ".transcript--transcript-panel--JLceZ",
    '[class*="transcript"][class*="panel"]',
    ".rc-Transcript",
    '[data-testid="transcript-container"]',
    "#transcript-scrollbox",
    "ytd-transcript-renderer",
    "ytd-transcript-segment-list-renderer",
  ];
  for (const selector of KNOWN) {
    for (const el of document.querySelectorAll(selector)) {
      if (!visible(el)) continue;
      const text = clean(el.innerText || "");
      const cues = text.split("\n").filter((l) => STAMP.test(l)).length;
      if (cues >= 2) {
        return { ok: true, text, method: `selector ${selector}`, cues };
      }
    }
  }

  // -- 2. the general path: wherever the timestamps actually are ---------------
  // Collect leaf-ish elements whose own text starts with a timestamp. Restricting to
  // elements with few children avoids matching the wrapper that contains all of them,
  // which would make every candidate the same ancestor and lose the signal.
  const matched = [];
  for (const el of document.querySelectorAll("div,span,p,li,button,td,section")) {
    if (el.childElementCount > 2) continue;
    const own = (el.textContent || "").trim();
    if (own.length > 0 && own.length < 400 && STAMP.test(own)) matched.push(el);
    if (matched.length > 4000) break;
  }

  // A cue often matches twice — the row, and the `<span>` holding just its timestamp.
  // Keeping both would double the count, which matters because the count is used as a
  // threshold below. Drop any element that contains another match.
  const stamped = matched.filter(
    (el) => !matched.some((other) => other !== el && el.contains(other)),
  );

  if (stamped.length < 3) {
    return {
      ok: false,
      method: "none",
      cues: stamped.length,
      note:
        "No transcript found on this page. Open the transcript panel first — on " +
        "Udemy it is the 'Transcript' button under the player, on Coursera it is the " +
        "'Transcript' tab. Then try again, or paste the text yourself.",
    };
  }

  // Lowest common ancestor of the stamped elements.
  const chain = (el) => {
    const path = [];
    for (let node = el; node; node = node.parentElement) path.unshift(node);
    return path;
  };
  let common = chain(stamped[0]);
  for (const el of stamped.slice(1)) {
    const path = chain(el);
    let i = 0;
    while (i < common.length && i < path.length && common[i] === path[i]) i += 1;
    common = common.slice(0, i);
    if (!common.length) break;
  }

  let container = common[common.length - 1] || document.body;

  // Some players put the timestamps in one column and the words in another, so the
  // common ancestor of the timestamps holds only bare numbers. The tell is how much
  // prose sits alongside each cue: a transcript averages a sentence, a stripe of bare
  // timestamps averages five characters. Climb only while that is what we are holding.
  //
  // Measuring prose-per-cue rather than counting matched elements matters: those are
  // different units, and comparing them made this climb all the way to <body> and
  // return the page header along with the transcript.
  const prosePerCue = (el) => {
    const lines = (el.innerText || "").split("\n").map((l) => l.trim()).filter(Boolean);
    const cues = lines.filter((l) => STAMP.test(l)).length;
    return cues ? lines.join(" ").length / cues : 0;
  };

  while (
    container.parentElement &&
    container !== document.body &&
    prosePerCue(container) < 30
  ) {
    container = container.parentElement;
  }

  const text = clean(container.innerText || "");
  const cues = text.split("\n").filter((l) => STAMP.test(l)).length;
  if (cues < 2) {
    return { ok: false, method: "density", cues, note: "Found timestamps but could not isolate the transcript." };
  }
  return { ok: true, text, method: "density", cues };
}

/** Injected into the page. What the tab is, and anything the user highlighted. */
export function pageContext() {
  const meta = (name) =>
    document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)?.content || "";
  return {
    url: location.href,
    title: (meta("og:title") || document.title || location.href).trim(),
    selection: (window.getSelection()?.toString() || "").trim(),
    hasTranscriptButton: /transcript/i.test(document.body.innerText.slice(0, 20000)),
  };
}
