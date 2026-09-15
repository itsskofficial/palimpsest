## What this changes

<!-- What was wrong or missing, and what it does now. -->

## How it was checked

<!-- Tests added, and anything run by hand: a capture, an apply, an undo. -->

## Checklist

- [ ] `ruff check src tests`, `mypy src`, `lint-imports` and `pytest` pass
- [ ] If this touches writing, undo or autonomy: `docs/safety.md` still holds, or is updated
- [ ] If this is a load-bearing decision: an ADR in `docs/decisions/`
- [ ] If users would notice: an entry under `## Unreleased` in `CHANGELOG.md`
- [ ] If the UI changed: `npm run export` in `clients/ui`, and the rebuilt `serve/static` is committed
