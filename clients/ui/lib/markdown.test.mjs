/**
 * The block parser behind the answer panel.
 *
 * `parse` is the half worth testing without a DOM: it decides what is a list, what is a
 * heading and where a paragraph ends, and every visible bug in this component so far has
 * been a grouping bug — a numbered answer collapsing into one run-on paragraph, or a
 * sub-bullet starting a second list because it was indented.
 *
 * The inline pass is checked here too, through the token regex, for the one case that is
 * a hazard rather than a cosmetic slip: a `javascript:` href must never become an anchor.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { INLINE, SAFE_HREF, parse, tokenise } from "./markdown-parse.mjs";


// ---------------------------------------------------------------------------
// grouping
// ---------------------------------------------------------------------------

test("a numbered answer is a list, not one run-on paragraph", () => {
  const blocks = parse("Here is what I found:\n\n1. Sleep and memory\n2. Spaced repetition");

  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].kind, "p");
  assert.equal(blocks[1].kind, "ol");
  assert.deepEqual(blocks[1].items, ["Sleep and memory", "Spaced repetition"]);
});

test("bullets group into one list", () => {
  const blocks = parse("- first\n- second\n- third");

  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].kind, "ul");
  assert.equal(blocks[0].items.length, 3);
});

test("an indented sub-bullet joins the list rather than starting a second one", () => {
  // This is exactly what the model writes: a numbered page, then indented details.
  const blocks = parse("- a page\n  - a detail about it\n  - another detail");

  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].items.length, 3);
});

test("a blank line ends a list so the next paragraph is its own block", () => {
  const blocks = parse("- one\n- two\n\nAnd a closing thought.");

  assert.equal(blocks.at(-1).kind, "p");
  assert.deepEqual(blocks.at(-1).lines, ["And a closing thought."]);
});

test("consecutive lines with no blank between them stay one paragraph", () => {
  const blocks = parse("A sentence that the model\nwrapped across two lines.");

  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].lines.length, 2);
});

test("empty paragraphs are dropped rather than rendered as gaps", () => {
  const blocks = parse("one\n\n\n\ntwo");

  assert.deepEqual(
    blocks.map((b) => b.lines),
    [["one"], ["two"]],
  );
});

test("headings are headings at every level the model uses", () => {
  const blocks = parse("# One\n## Two\n### Three");

  assert.deepEqual(
    blocks.map((b) => [b.kind, b.level, b.text]),
    [
      ["h", 1, "One"],
      ["h", 2, "Two"],
      ["h", 3, "Three"],
    ],
  );
});

test("a hash with no space is not a heading", () => {
  // `#1 on the list` is prose, and swallowing it loses the line.
  const blocks = parse("#1 on the list");

  assert.equal(blocks[0].kind, "p");
});

test("windows line endings parse the same as unix ones", () => {
  assert.deepEqual(parse("- a\r\n- b"), parse("- a\n- b"));
});

test("an empty reply is no blocks rather than one empty one", () => {
  assert.deepEqual(parse(""), []);
  assert.deepEqual(parse("\n\n  \n"), []);
});

// ---------------------------------------------------------------------------
// inline
// ---------------------------------------------------------------------------

function tokens(text) {
  INLINE.lastIndex = 0;
  return [...text.matchAll(INLINE)].map((m) => m[0]);
}


function kinds(text) {
  return tokenise(text).map((t) => t.type);
}

test("bold, italic, code and links are all recognised", () => {
  assert.deepEqual(tokens("**b** and *i* and `c` and [t](https://x.test/p)"), [
    "**b**",
    "*i*",
    "`c`",
    "[t](https://x.test/p)",
  ]);
});

test("a file url is a link, because a vault page is a file", () => {
  assert.deepEqual(tokens("[Sleep](file:///notes/sleep.md)"), [
    "[Sleep](file:///notes/sleep.md)",
  ]);
});

test("a javascript href is never matched as a link", () => {
  // The one real hazard in rendering model output. It stays as literal characters.
  assert.deepEqual(tokens("[click](javascript:alert(1))"), []);
  assert.equal(SAFE_HREF.test("javascript:alert(1)"), false);
  assert.equal(SAFE_HREF.test("data:text/html,<script>"), false);
  assert.equal(SAFE_HREF.test("https://notion.so/p"), true);
});

test("an unmatched asterisk is left alone rather than eating the rest of the line", () => {
  assert.deepEqual(tokens("2 * 3 is 6"), []);
});

test("emphasis does not run across a line break", () => {
  assert.deepEqual(tokens("*not\nemphasis*"), []);
});


test("plain text between tokens is kept in order", () => {
  const parts = tokenise("before **bold** after");

  assert.deepEqual(kinds("before **bold** after"), ["text", "bold", "text"]);
  assert.equal(parts[0].value, "before ");
  assert.equal(parts[1].value, "bold");
  assert.equal(parts[2].value, " after");
});


test("a refused href stays as the literal characters", () => {
  const parts = tokenise("[click](javascript:alert(1))");

  assert.deepEqual(parts.map((t) => t.type), ["text"]);
  assert.equal(parts[0].value, "[click](javascript:alert(1))");
});


test("a link keeps its label and its href apart", () => {
  const [link] = tokenise("[Sleep and memory](https://notion.so/p)");

  assert.equal(link.type, "link");
  assert.equal(link.label, "Sleep and memory");
  assert.equal(link.href, "https://notion.so/p");
});
