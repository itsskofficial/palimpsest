/**
 * The small slice of Markdown the agent actually writes, rendered.
 *
 * The answer panel used to print the reply into a `<p>`, so it came out with its
 * asterisks showing — `**Sleep and memory**`, literally — and a numbered answer as one
 * run-on paragraph. That is the surface where somebody reads what their own notes say,
 * so it is the last place raw markup belongs.
 *
 * Hand-rolled rather than pulled in, for the same reason the metrics endpoint is:
 * `react-markdown` plus a sanitiser is a large dependency and a sanitisation decision,
 * for a grammar two small files cover. The grouping and tokenising live next door in
 * `markdown-parse.mjs` so they can be tested without a DOM; this file is only the JSX.
 *
 * Nothing here interpolates HTML. Every value becomes a React text node, so a reply that
 * happens to contain `<script>` renders as those characters and nothing else.
 */
import { Fragment, type ReactNode } from "react";
import { parse, tokenise, type Token } from "@/lib/markdown-parse";

function inline(text: string, keyPrefix: string): ReactNode[] {
  return tokenise(text).map((token, i) => render(token, `${keyPrefix}-${i}`));
}

/** One token. Emphasis renders its children, so `**[Title](url)**` stays a link. */
function render(token: Token, key: string): ReactNode {
  switch (token.type) {
    case "link":
      return (
        <a
          key={key}
          href={token.href}
          target="_blank"
          rel="noreferrer noopener"
          className="text-sepia underline decoration-rule underline-offset-2 transition hover:decoration-sepia"
        >
          {token.label}
        </a>
      );
    case "bold":
      return (
        <strong key={key} className="font-semibold text-ink">
          {token.children.map((child, j) => render(child, `${key}-${j}`))}
        </strong>
      );
    case "code":
      return (
        <code
          key={key}
          className="rounded bg-raised px-1 py-0.5 font-mono text-[13px] text-soft"
        >
          {token.value}
        </code>
      );
    case "italic":
      return (
        <em key={key} className="italic">
          {token.children.map((child, j) => render(child, `${key}-${j}`))}
        </em>
      );
    default:
      return <Fragment key={key}>{token.value}</Fragment>;
  }
}

/** Render a reply. Unknown syntax survives as the characters it was written with. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="space-y-3 text-[15px] leading-relaxed text-ink">
      {parse(text).map((block, i) => {
        if (block.kind === "h") {
          const size = block.level <= 2 ? "text-base" : "text-[15px]";
          return (
            <p key={i} className={`font-display ${size} font-semibold text-ink`}>
              {inline(block.text, `h${i}`)}
            </p>
          );
        }
        if (block.kind === "ul") {
          return (
            <ul key={i} className="list-disc space-y-1 pl-5 marker:text-faint">
              {block.items.map((item, j) => (
                <li key={j}>{inline(item, `${i}-${j}`)}</li>
              ))}
            </ul>
          );
        }
        if (block.kind === "ol") {
          return (
            <ol key={i} className="list-decimal space-y-1 pl-5 marker:text-faint">
              {block.items.map((item, j) => (
                <li key={j}>{inline(item, `${i}-${j}`)}</li>
              ))}
            </ol>
          );
        }
        return (
          <p key={i}>
            {block.lines.map((line, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(line, `${i}-${j}`)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}
