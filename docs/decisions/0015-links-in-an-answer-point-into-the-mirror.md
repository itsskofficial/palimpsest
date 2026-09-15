# 15. A link in an answer must point at a page the mirror knows

**Status:** Accepted
**Date:** 2026-09-12

## Context

The Ask panel says answers come from what you have written, with page links. The agent's
tools return each page's `title` and `url`, and the system prompt tells the model to cite a
page as `[Title](url)` rather than by its internal id.

Asked to do that, a local 7B model sometimes wrote a plausible URL instead of copying the
one it was given. In one answer it produced three `https://example.com/...` links, one of
them for a page that does not exist.

A fabricated link is worse than no link. A page id is visibly internal, so nobody mistakes
it for a checked reference. A link looks checked, and the reader has no way to tell the
difference without clicking it.

## Decision

After every turn, `ground_links` rewrites the reply. A Markdown link survives only if its
target is a page URL in the mirror. Any other link keeps its text and loses its target.

If the mirror is empty there is nothing to check against, so the reply is left unchanged.
Bare URLs in prose are left alone, since they are usually quoted from a source rather than
offered as citations.

The renderer is the second layer. It only turns `http`, `https`, `file` and root-relative
targets into anchors, and it never interprets HTML, so a reply containing markup renders as
text.

## Rejected

**Trusting the prompt.** It works for capable models and fails silently for weaker ones,
which are exactly the models someone runs with no key.

**Rendering citations from tool results instead of prose.** Showing a sources row built from
the pages the turn actually read is more robust, and may come later. It needs a structured
response from the agent loop, while grounding the prose fixes the reply as written today.

**A Markdown library with a sanitiser.** Sanitising decides what is safe to render. It does
not decide what is true, and the fabricated links were well-formed.

## Consequences

A citation in an answer is either a page in your knowledge base or plain text. A real link
is unaffected. An invented one loses its authority and nothing else.

The check runs on the server, so the Telegram bot's replies are grounded the same way as
the desktop app's.

A page whose URL changed since the last sync will have its link demoted until the mirror is
refreshed. That is the right direction to fail in.
