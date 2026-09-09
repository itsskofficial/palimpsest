# 2. Contradictions are never applied automatically

**Status:** Accepted
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
