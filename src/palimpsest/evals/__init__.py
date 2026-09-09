"""Evaluation: the ground truth that turns "it feels right" into a number.

The suites in this package, and what each one can tell you:

- `retrieval` — recall@k over the committed fixture. **Needs no model and no key**, so it
  runs in CI: a change that makes ranking worse fails the build. It goes first because
  the classifier can only choose among the pages retrieval handed it, and a page that was
  never retrieved cannot be corroborated — the claim becomes a new page instead, which is
  the exact failure this product exists to prevent.
- `component` — per-relation precision/recall for the seven-relation classifier, with
  contradiction recall weighted 3× because a missed contradiction corrupts the base.
  Needs a model, so it is a command you run.
- `safety` — the invariants that must hold at 100%. These live in
  `tests/unit/test_safety.py` rather than here, because a merge gate has to be
  deterministic and offline.
- `report` — a scorecard, recorded to the store and to Langfuse.

Two sources of labelled data, and they answer different questions. `fixture` ships with
the package: the same notes and the same questions on every machine, so a number is
comparable between runs and a fresh clone can be measured at all. `golden` grows from the
user's own Approve/Reject decisions — labelled data at no extra cost, measuring the system
against the notes it actually lives in.
"""

from __future__ import annotations

__all__ = ["component", "fixture", "golden", "report", "retrieval"]
