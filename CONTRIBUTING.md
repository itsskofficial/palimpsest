# Contributing

Thanks for looking. This is a tool that edits people's notes, so the bar for a change is
mostly about not making that less safe. The rest is ordinary.

## Setting up

```bash
git clone https://github.com/itsskofficial/palimpsest
cd palimpsest
pip install -e ".[dev]"
pytest
```

The default suite runs offline with no keys. It uses SQLite, a scripted model, and a
markdown vault in a temp directory, so the real write path and undo are exercised without
a Notion account.

For the clients:

```bash
cd clients && npm install && npm test      # browser extension and desktop shell
cd clients/ui && npm install && npm run dev  # the interface, against `palimpsest serve`
```

## Checks

CI runs these, and a pull request should pass all of them:

```bash
ruff check src tests scripts
mypy src/palimpsest
lint-imports          # the architectural contracts, including the one write door
pytest
```

Some tests are opt-in because they need something external:

| Marker | Needs | Run with |
|---|---|---|
| `postgres` | a Postgres at `PALIMPSEST_TEST_POSTGRES` | `PALIMPSEST_TEST_POSTGRES=postgresql://… pytest` |
| `api` | a live model key and network | `pytest -m api` |
| `browser` | Chrome and a running server | `pytest tests/e2e -m browser` |

The store tests run against both SQLite and Postgres when the variable is set, and assert
the two behave the same.

## The interface ships inside the Python package

`pip install palimpsest-notion` needs no Node, because the built UI is committed to
`src/palimpsest/serve/static/`. After changing anything in `clients/ui`:

```bash
cd clients/ui && npm run export
```

and commit the rebuilt `static/` directory with your change.

## What reviews look for

- **The one write door holds.** Only `notion/apply.py` writes to a workspace, and
  `lint-imports` enforces it. A change that needs to write goes through a patch and the
  approval gate.
- **Everything applied can be undone exactly.** Each operation records its inverse before
  it runs. A new operation kind needs its inverse and a test that undo restores the
  original.
- **Contradictions are never applied automatically.** No setting changes that.
- **Failure is legible.** An error says what went wrong and what to do about it, in the
  terms of the backend that is actually configured.
- **Tests describe the failure they prevent.** Test names read as the behaviour, and the
  docstring says what would break for a user if it regressed.

[docs/safety.md](docs/safety.md) lists each guarantee and the test that enforces it. If a
change affects one, update that document in the same pull request.

## Decisions and changes

A load-bearing design choice gets an architecture decision record in
[`docs/decisions/`](docs/decisions/README.md): the context, the decision, what was
rejected, and what it costs. User-visible changes get a line under `## Unreleased` in
[`CHANGELOG.md`](CHANGELOG.md).

## Releasing

Maintainers only. Move the `Unreleased` section in `CHANGELOG.md` under a version heading,
bump the version in `pyproject.toml`, `src/palimpsest/_version.py` and the client
manifests, then push a `v*` tag. The release workflow builds the installers, packs the
extension, publishes to PyPI, and uses the changelog section as the release notes.
