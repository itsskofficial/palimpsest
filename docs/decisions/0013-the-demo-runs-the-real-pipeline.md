# 13. The demo runs the real pipeline, and its promises are tested

**Status:** Accepted
**Date:** 2026-09-12

## Context

`palimpsest demo` needs to work with nothing configured, because its entire job is to be
the thing somebody runs before deciding whether to configure anything. The obvious way to
guarantee that is to record a set of model responses and replay them: no key, no network,
no variance, works forever.

It is also a lie, and a brittle one. A recorded demo shows the output of a pipeline rather
than a pipeline. It breaks the instant a visitor types something of their own — which is
the first thing anyone curious does — and the failure is the worst kind, because up to
that point they believed what they saw. A project whose demo is a recording is a project
whose README you stop trusting.

The counter-problem: a real pipeline means a real model, which means a key.

## Decision

The demo is the ordinary pipeline against the markdown backend (ADR 11). Same mirror, same
retrieval, same classifier, same planner, same write door, same undo. Nothing is stubbed
and no output is recorded.

For the model it degrades in the open: use whatever is configured; failing that, look for
a local Ollama, which needs no key and no account; failing that, **start anyway** and say
plainly that nothing will be classified. The vault, retrieval, the duplicate sweep and
undo all work with no model at all, so the last case is still a working product rather
than an error screen.

Four suggested captures ship with it, each stating what it will do. Those statements are
tested against a live model by `tests/demo/test_promises.py`.

## Consequences

### What this buys

What a visitor sees is what they get. Typing their own text works exactly as well as
clicking a suggestion, because there is no difference between the two paths.

The promises stay true. The vault is fixed but the model is not, and a demo that claims
"this one will argue with a page" and then does something else is worse than no demo. That
test is the only way to know, and it has already earned itself: on the first run, **all
four promises failed**.

Two failed because the model was right and the sample pages did not say what the prompts
assumed — the gradient clipping page called global-norm "the default" rather than "the
best", so nothing contradicted it. Two failed because of real product bugs, one of which
(ADR 12) was a design flaw that had been quietly filling the review queue all along.

### What this costs

**Ollama on a laptop is slow.** A capture that takes 8 seconds on a hosted model takes
around 80 on a local 7B. The demo says which model it is using, which is the least it can
do.

**A small local model classifies worse.** Measured, some are much worse. A visitor whose
only model is a 7B may see a weaker verdict than the suggestion promised, and there is no
way to be both honest and flattering here — the eval table in the README is the answer,
and it names names.

**The promise tests cost money and cannot run in the merge gate.** They are marked `api`
and are a release-time check, so a regression can land and sit until someone runs them.

### A related trap, recorded because it cost an hour

The hermetic test fixture strips provider keys so that a developer's shell cannot make a
test pass that would fail in CI. Correct — but under it the demo tests found no key, fell
back to the local Ollama, and reported on a 7B model while claiming to report on the
configured one. They did not fail; they passed and disagreed with the same prompt run by
hand. Tests marked `api` are now exempt, because reaching a real provider is their whole
purpose.

The general shape: **a fallback that is a kindness at runtime is a hazard in a test**,
because it converts "this is misconfigured" into "this quietly measured something else".
