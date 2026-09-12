---
title: "Scaling laws"
icon: "📈"
---

# Scaling laws

Model loss falls as a power law in three quantities: parameters, data, and compute.
The practical consequence is that performance is largely predictable in advance,
which is unusual and is why the field became willing to spend so much on single runs.

## The shape of it

Plotted on log-log axes the relationship is a straight line over several orders of
magnitude. That straightness is the whole result — it means an experiment at small
scale tells you something reliable about a run you have not done yet.

## Compute-optimal training

For a fixed compute budget there is an optimal split between model size and number
of training tokens. Early large models were substantially undertrained by this
measure: too many parameters, not enough data.
