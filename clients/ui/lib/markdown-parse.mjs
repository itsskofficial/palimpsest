/**
 * Grouping and tokenising for the answer panel's Markdown — the half with no JSX in it.
 *
 * Separate from `markdown.tsx` so it can be run directly by `node --test`, which cannot
 * load TSX. That is not a contrivance: every visible bug in this component has been a
 * grouping bug rather than a styling one — a numbered answer collapsing into one run-on
 * paragraph, a sub-bullet starting a second list because it was indented — and those are
 * exactly the things testable without a DOM.
 *
 * The grammar is deliberately the one the model writes in a reply: bold, italic, inline
 * code, links, bullets, numbers, headings, paragraphs. No HTML passthrough, no images,
 * no tables. Anything unrecognised stays as the characters it was written with, which is
 * the safe direction to fail in.
 */

/** `[text](url)`, `**bold**`, `` `code` ``, `*italic*` — one pass, so they interleave. */
export const INLINE =
  /(\[[^\]\n]+\]\((?:https?:\/\/|file:\/\/|\/)[^)\s]+\))|(\*\*[^*\n]+\*\*)|(`[^`\n]+`)|(\*[^*\n]+\*|_[^_\n]+_)/g;

/** Only these become anchors. A `javascript:` or `data:` href is the one real hazard. */
export const SAFE_HREF = /^(https?:\/\/|file:\/\/|\/)/i;

/**
 * @typedef {{kind: "p", lines: string[]}
 *   | {kind: "ul", items: string[]}
 *   | {kind: "ol", items: string[]}
 *   | {kind: "h", level: number, text: string}} Block
 */

/**
 * Group lines into blocks. A blank line separates; a list marker starts or continues.
 *
 * @param {string} source
 * @returns {Block[]}
 */
export function parse(source) {
  /** @type {Block[]} */
  const blocks = [];

  for (const raw of String(source ?? "").replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    const previous = blocks[blocks.length - 1];

    if (!line.trim()) {
      // A blank line closes whatever was open, so the next line starts fresh. The empty
      // paragraph it leaves behind is dropped at the end rather than rendered as a gap.
      if (previous) blocks.push({ kind: "p", lines: [] });
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: "h", level: heading[1].length, text: heading[2] });
      continue;
    }

    // Matched after any leading space: the model indents sub-bullets, and treating an
    // indented one as a new list breaks a single answer into several.
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    if (bullet) {
      if (previous?.kind === "ul") previous.items.push(bullet[1]);
      else blocks.push({ kind: "ul", items: [bullet[1]] });
      continue;
    }

    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      if (previous?.kind === "ol") previous.items.push(numbered[1]);
      else blocks.push({ kind: "ol", items: [numbered[1]] });
      continue;
    }

    if (previous?.kind === "p") previous.lines.push(line);
    else blocks.push({ kind: "p", lines: [line] });
  }

  return blocks.filter((b) => (b.kind === "p" ? b.lines.length > 0 : true));
}

/**
 * Split one line into text and marked-up runs, in order.
 *
 * @param {string} text
 * @returns {({type: "text", value: string}
 *   | {type: "link", label: string, href: string}
 *   | {type: "bold" | "italic" | "code", value: string})[]}
 */
export function tokenise(text) {
  const out = [];
  let last = 0;
  let match;
  INLINE.lastIndex = 0;

  while ((match = INLINE.exec(text)) !== null) {
    if (match.index > last) {
      out.push({ type: "text", value: text.slice(last, match.index) });
    }
    const token = match[0];

    if (token.startsWith("[")) {
      const split = token.indexOf("](");
      const href = token.slice(split + 2, -1);
      out.push(
        SAFE_HREF.test(href)
          ? { type: "link", label: token.slice(1, split), href }
          : { type: "text", value: token },
      );
    } else if (token.startsWith("**")) {
      out.push({ type: "bold", value: token.slice(2, -2) });
    } else if (token.startsWith("`")) {
      out.push({ type: "code", value: token.slice(1, -1) });
    } else {
      out.push({ type: "italic", value: token.slice(1, -1) });
    }
    last = match.index + token.length;
  }

  if (last < text.length) out.push({ type: "text", value: text.slice(last) });
  return out;
}
