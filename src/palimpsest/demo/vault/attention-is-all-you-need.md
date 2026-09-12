---
title: "Attention is all you need"
icon: "📄"
---

# Attention is all you need

> [!abstract] Vaswani et al., 2017
> The paper that introduced the transformer. Replaces recurrence entirely with
> self-attention, which made training parallel across sequence positions.

## The argument

Recurrent models process a sequence one position at a time, so training cannot be
parallelised along the sequence. Self-attention computes all positions at once, and
the paper's claim is that this is not merely faster but *sufficient* — you do not
need recurrence at all.

## What I took from it

- Attention is a soft lookup: queries against keys, weighted sum of values.
- Positional information has to be added back, because attention itself is
  permutation-invariant.
- Multiple heads let the model attend to several relationships at once.

## What it does not say

The paper is about translation. It makes no claim about scaling behaviour, and the
scaling results everyone now cites came years later.
