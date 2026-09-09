# 2. Contradictions are never applied automatically

**Status:** Amended by the section at the foot of this file (2026-09-09)
**Date:** 2026-09-09

## Context

Six of the seven relations are wrong in recoverable ways. A bad `corroborates` adds a
citation to the wrong sentence; you notice, you undo it, nothing is lost. `contradicts`
is different in kind: applying it means replacing something you believe with something
the source asserts, so getting it wrong silently replaces a true claim with a false one.

The damage is not the one wrong sentence. It is that you stop knowing which parts of the
base to trust, which makes the automation worse than none at all. The tempting compromise
is a confidence threshold. Precision on the other six is *earned* by measurement against
your accept/reject history, and a threshold is the right shape for an earned property.
This one is a policy, and policies do not have thresholds.

## Decision

`Relation.CONTRADICTS.auto_appliable` returns `False` unconditionally. `AUTONOMY_LEVELS`
in `config.py` is `{"none": set(), "low": {"low"}, "medium": {"low", "medium"}}` — there
is no `high`, so the tier `contradicts` carries is in no level's set, and
`Settings.validate` says so when someone tries to set one.

Three locks on the same door, deliberately redundant: `plan.py` emits no operation for a
contradiction at any confidence and routes it to `review` with both sides;
`approval.gate` checks the relation before anything else and puts it in `blocked`, so it
becomes neither an auto-apply nor even a held approval; the apply route refuses one if it
somehow arrives. `may_auto_apply` also consults `PALIMPSEST_APPLY`, so writes-off vetoes
everything above all of it. The `safety` job in CI asserts each property directly rather
than trusting it.

## Consequences

### What this buys

The one failure that would end the project cannot happen through any surface. The agent
cannot apply a contradiction, nor the bot, nor the job handler, and none can be argued
into it, because the limits are in code rather than in a prompt.
`tests/unit/test_safety.py` pins the whole autonomy matrix offline, so the gate is
deterministic rather than a hope about model behaviour.

### What this costs

Contradictions are the highest-value thing the classifier finds and they always require
you. If your base holds many — and `sweep contradictions` suggests it does — the queue is
where they sit, and the product is doing manual work on exactly the cases you most wanted
help with. There is no setting to trade that away, which some users will want and will
not get.

Three locks also means three places to keep correct, with duplicated tests.

## Revisit if

Nothing. This is the one decision with no revisit condition: a measured precision number,
however good, does not change a policy about which failures are acceptable. If
contradictions should be resolved faster, the answer is a better review surface — both
sides side by side, one tap — not automation.


---

## Amendment — `autonomy=everything` (2026-09-09)

The section above ends "Revisit if: Nothing", and that was too broad a claim. It
conflated two powers that turn out to be separable:

- **resolving** a contradiction — deciding which of two sourced claims is true, and
  editing the notes to match;
- **recording** one — writing down that two sources disagree, with both sides visible.

The reasoning above is entirely about the first. Every sentence of it — the silent
replacement of a true claim, the loss of trust in the parts still right, why a confidence
threshold is the wrong shape — describes a system that picks a winner. None of it argues
against writing "these two disagree" into the page.

So the ladder gained one rung, `everything`, and what it applies is a *record*: one
`append_block` placed directly after the contradicted sentence, carrying the competing
claim and its source, marked as a conflict. The existing sentence is not edited, struck
or archived. The operation inverts by removing one block.

**What is unchanged.** The system still never resolves a contradiction, at any setting.
No level edits a sentence on the strength of a source that disagrees with it. Below
`everything` the behaviour is exactly as described above, and `everything` must be typed
in full — it is not reachable by degrees.

**What this costs.** A wrong `contradicts` now puts a red callout in a page you did not
ask to have marked up. That is visible, cited and reversible in one tap, but it is still
your notes being written in by a machine that was wrong. The confidence floor applies —
an unsure contradiction still waits — so the failure needs the classifier to be
confidently wrong, which the eval measures at 100% recall and 100% precision on the
committed fixture and cannot promise on yours.

**Why it was made.** Asked for explicitly, twice, by the person whose notes these are,
alongside two conditions that change the calculus: every action is undoable by a human,
and every action is listed in one place. Autonomy is safe in proportion to how cheap it
is to reverse. The original decision was written for a system with no activity log and no
undo button in the interface; both now exist.

## Revisit if

Contradiction precision on a real workspace falls below the fixture's, or a user reports
a red callout on a page where the two claims did not actually conflict. The rung is
opt-in, so the remedy is a better default rather than a removed capability.
