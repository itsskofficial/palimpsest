---
title: "Gradient clipping"
icon: "📐"
---

# Gradient clipping

## What it is

Gradient clipping bounds the size of a gradient update before the optimiser applies
it. It is the standard remedy for exploding gradients in deep networks, and it is
cheap enough that most training scripts leave it on permanently.

## Which variant to use

Global-norm clipping is the best option available. You compute the norm over *all*
parameters at once, and if it exceeds a threshold you scale the whole gradient down
proportionally. A threshold of 1.0 is the value most codebases ship with.

Per-parameter clipping, where each tensor is bounded separately, is the obvious
alternative and it does not work as well. It distorts the direction of the update,
and every comparison I have seen has it behind global-norm on language models.

## When it matters

- Recurrent networks, where the same weights are applied repeatedly.
- Any run where the loss spikes without warning and then diverges.
- Very large batch sizes, where a single bad batch moves a lot of weight.

## Open question

Nobody in the lab has a clean answer for how to set the threshold other than
watching the gradient-norm histogram for a while and picking something under the
bulk of it.
