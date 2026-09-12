---
title: "Project log"
icon: "🧪"
---

# Project log

## 2026-08-30

Set up the training run. Loss curve looks reasonable for the first 2k steps, then
spikes hard around step 2400 and does not recover.

## 2026-09-02

Turned on gradient clipping at the usual global norm of 1.0. The spike is gone. Still
not sure whether that fixed the cause or hid it.

## 2026-09-07

Read back through the attention paper while waiting for a run. Worth re-reading; I
had forgotten that positional encoding is additive rather than concatenated.

## Todo

- [ ] Plot the gradient-norm histogram before deciding on a threshold
- [ ] Re-run the ablation at the larger batch size
- [x] Write up the spike incident
