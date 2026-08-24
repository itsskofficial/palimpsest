"""Evaluation: the ground truth that turns "it feels right" into a number.

Four suites, built out in this package:

- `component` — per-relation precision/recall over a hand-labelled golden set, with
  contradiction recall weighted heavily because a missed contradiction corrupts the base.
- `trajectory` — did the agent choose the right tool, stay grounded, recover from errors.
- `safety` — an adversarial suite that must pass 100% before the gate is trusted.
- `report` — a scorecard, synced to Langfuse.

The golden set lives in the store (`eval_examples`) and grows for free from every
Approve/Reject the user makes — labelled data at no extra cost.
"""

from __future__ import annotations

__all__: list[str] = []
