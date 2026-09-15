/**
 * Types for `markdown-parse.mjs`.
 *
 * The parser is plain JavaScript so `node --test` can import it without a build step —
 * it is the half of the Markdown renderer worth testing, and TSX cannot be loaded
 * directly. `tsconfig.json` only includes `.ts`/`.tsx`, so the types come from here
 * rather than from the JSDoc in the module itself.
 */

export type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "h"; level: number; text: string };

export type Token =
  | { type: "text"; value: string }
  | { type: "link"; label: string; href: string }
  | { type: "bold"; value: string; children: Token[] }
  | { type: "italic"; value: string; children: Token[] }
  | { type: "code"; value: string };

export declare const INLINE: RegExp;
export declare const SAFE_HREF: RegExp;

/** Group lines into blocks: blank lines separate, list markers start and continue. */
export declare function parse(source: string): Block[];

/** Split one line into text and marked-up runs, in order. */
export declare function tokenise(text: string): Token[];
