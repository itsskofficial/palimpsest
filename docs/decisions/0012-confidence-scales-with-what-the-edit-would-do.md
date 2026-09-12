# 12. The confidence bar scales with what the edit would do

**Status:** Accepted
**Date:** 2026-09-12

## Context

`PALIMPSEST_MIN_CONFIDENCE` was one number, default `0.75`, applied to every judgement.
Below it, the claim became a review item; above it, an operation. Simple, and wrong in a
way that only showed up when the demo was built and four prepared captures were run
against a real model on a real vault.

Three of the four produced nothing. The classifier was not confused — the rationales were
correct and specific. It reported `new` at **0.62**, consistently, which is a perfectly
calibrated number for the judgement it was making.

That is the whole problem. The relations are not the same *kind* of question:

- "Does this claim agree with line 12?" compares two sentences. A model can be very sure.
- "Does this need a page that does not exist yet?" is a judgement about an entire
  knowledge base — everything it might already relate to, and whether none of it fits.
  A model that reports 0.95 for that is not being confident, it is being careless.

So a well-calibrated model is *least* certain about the judgement behind the **safest**
operation. A flat bar therefore rejects a purely additive block — one that undoes in a
click — most often, while passing a strike-through, which changes what an existing
sentence means, on the same number. Precisely backwards, and invisible: nothing errors,
the review queue just quietly fills with the wrong things.

## Decision

The bar is a multiplier on the setting, keyed on the operation's existing risk tier:

```python
CONFIDENCE_BY_RISK = {"low": 0.8, "medium": 1.0, "high": 1.2}
```

At the default this is **0.60** to add a block, **0.75** to rewrite a sentence, **0.90** to
change what an existing sentence means. `PALIMPSEST_MIN_CONFIDENCE` still moves the whole
scale, so raising it tightens everything and the knob keeps meaning what it used to.

The risk tiers already existed and already gate the autonomy ladder, so this reuses the
project's one notion of "how bad would being wrong be" rather than inventing a second.

## Consequences

### What this buys

`new` — the most common verdict on a young knowledge base, and the most reversible —
applies rather than piling into a review queue nobody reads. A review queue that fills
with safe, obvious additions is how people stop reading review queues, and after that the
dangerous items go through unread too.

High-risk edits got *stricter*, not looser. The change is not a relaxation on net.

### What this costs

**More writes happen automatically.** That is the point, and it is bounded by what those
writes are: at 0.60 the only things passing are appends and citations, every one of which
is a single reversible block with its reasoning on the page.

**Three numbers instead of one**, and they are a judgement rather than a measurement. They
were chosen to bracket what a calibrated model actually reports, which is a weaker
justification than a calibration curve, and the honest version of this ADR says so.

**A miscalibrated model is affected differently.** A model that reports 0.9 for everything
is not saved by any of this — which is what `palimpsest eval component` is for, and why
`status` refuses to be quiet about an unmeasured model.

### What would overturn it

Measuring per-relation calibration across several models and finding the ordering does not
hold — that is, that models are not systematically less confident about `new` than about
`corroborates`. The golden set makes that measurable; it has not been measured yet, and
until it is, these three numbers are a defensible guess and nothing more.
