/**
 * The transcript finder, against DOM shapes that mirror the real ones.
 *
 * What is genuinely being tested is the *density* path — the one that has to work when
 * a platform ships a redesign and every selector stops matching. The Coursera fixture
 * therefore has no recognisable class names at all: the cues sit three levels deep
 * inside generic `div`s, surrounded by page furniture that also contains timestamps
 * ("0:00 saved" in the header, "1:1 support" in a footer). Finding the transcript in
 * that is the whole job.
 *
 * `linkedom` has no layout, so it has no `innerText`. The shim below reproduces the
 * only part of it this code depends on: block-level elements introduce line breaks.
 * That is a fair stand-in, because the scraper's contract with the page is exactly
 * "give me the visible text with its line structure intact".
 *
 *     node --test clients/extension/test/
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { parseHTML } from "linkedom";

import { pageContext, scrapeTranscript } from "../scrape.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const BLOCK = new Set([
  "DIV", "P", "LI", "UL", "OL", "SECTION", "HEADER", "FOOTER", "NAV", "ASIDE",
  "MAIN", "H1", "H2", "H3", "H4", "TR", "TD", "ARTICLE", "BUTTON",
]);

/** Load a fixture and install the globals the injected function expects. */
function load(name) {
  const html = readFileSync(join(HERE, "fixtures", name), "utf8");
  const { document, window } = parseHTML(html);

  Object.defineProperty(window.Element.prototype, "innerText", {
    configurable: true,
    get() {
      const walk = (node) => {
        let out = "";
        for (const child of node.childNodes) {
          if (child.nodeType === 3) out += child.textContent;
          else if (child.nodeType === 1) {
            const isBlock = BLOCK.has(child.tagName);
            if (isBlock) out += "\n";
            out += walk(child);
            if (isBlock) out += "\n";
          }
        }
        return out;
      };
      return walk(this).replace(/[ \t]+/g, " ").replace(/\n{2,}/g, "\n").trim();
    },
  });

  globalThis.document = document;
  globalThis.window = window;
  globalThis.location = { href: `https://example.test/${name}` };
  window.getComputedStyle = () => ({ display: "block", visibility: "visible" });
  window.getSelection = () => ({ toString: () => "" });
  return document;
}

const stamps = (text) =>
  text.split("\n").filter((l) => /^\s*[[(]?\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\b/.test(l));

test("finds a Udemy transcript through its stable container", () => {
  load("udemy.html");
  const found = scrapeTranscript();

  assert.equal(found.ok, true);
  assert.match(found.method, /selector/);
  assert.equal(found.cues, 4);
  // The words must survive, not just the timestamps.
  assert.match(found.text, /Vaswani and colleagues/);
  // And the page furniture must not: a course sidebar full of durations would
  // otherwise be extracted as if it were speech.
  assert.doesNotMatch(found.text, /My learning/);
  assert.doesNotMatch(found.text, /Course content/);
});

test("finds a Coursera transcript with no usable selectors, by density alone", () => {
  load("coursera.html");
  const found = scrapeTranscript();

  assert.equal(found.ok, true);
  assert.equal(found.method, "density");
  assert.equal(found.cues, 4);
  assert.match(found.text, /Lipschitz constant/);
  // "0:00 saved" in the header is a timestamp-shaped decoy outside the transcript.
  assert.doesNotMatch(found.text, /saved/);
});

test("climbs out of a bare timestamp column to reach the words", () => {
  // The ancestor of the timestamps is `.times`, which contains no speech. Stopping
  // there yields four numbers and no transcript — technically "found", entirely
  // useless, and exactly the silent failure this heuristic exists to avoid.
  load("split-columns.html");
  const found = scrapeTranscript();

  assert.equal(found.ok, true);
  assert.equal(found.cues, 4);
  assert.match(found.text, /KL divergence/);
  assert.match(found.text, /0:04/);
  // Having climbed, it must still stop before the page header.
  assert.doesNotMatch(found.text, /elapsed/);
});

test("the scraped text is in a shape the Python parser accepts", () => {
  // The browser half's only real contract: hand back lines the tested parser can read.
  for (const name of ["udemy.html", "coursera.html"]) {
    load(name);
    const found = scrapeTranscript();
    const lines = stamps(found.text);
    assert.ok(lines.length >= 3, `${name} produced ${lines.length} timestamped lines`);
  }
});

test("an hour-long stamp is preserved verbatim for the parser to interpret", () => {
  load("udemy.html");
  assert.match(scrapeTranscript().text, /1:12:34/);
});

test("a page with no transcript says what to do instead of failing silently", () => {
  const { document, window } = parseHTML(
    "<body><h1>An article</h1><p>No timestamps here at all.</p></body>",
  );
  globalThis.document = document;
  globalThis.window = window;
  window.getComputedStyle = () => ({ display: "block", visibility: "visible" });

  const found = scrapeTranscript();
  assert.equal(found.ok, false);
  assert.match(found.note, /transcript panel/i);
});

test("pageContext prefers the og:title over the tab title", () => {
  load("udemy.html");
  const context = pageContext();
  // The tab title is "Mastering Transformers | Udemy" — the course. The og:title is
  // the lecture, which is what a citation should name.
  assert.equal(context.title, "Attention Is All You Need — Lecture 4");
});
