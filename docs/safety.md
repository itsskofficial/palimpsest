# What this is allowed to do to your notes

You are considering giving an agent write access to a knowledge base you have spent years
building. That is a large ask, and "trust me, it has tests" is not an answer. This
document is the answer: every limit the system places on itself, stated plainly, with a
pointer to the test that enforces it.

Every invariant here is checked by [`tests/unit/test_safety.py`](../tests/unit/test_safety.py).
That file runs **offline, with no API key, on every commit**. It is deliberately not a
suite of prompts fired at a live model and graded — an adversarial prompt is a useful
online eval, but a merge gate has to be deterministic, so each test pins a *structural*
guarantee rather than hoping a model behaves.

If any test in that file fails, the agent can do something it must not, and the fix is
never to change the test.

---

## The short version

1. **Nothing is written unless you switch writing on.** `PALIMPSEST_APPLY=0` is the
   default and is an absolute veto — no autonomy setting overrides it.
2. **Nothing is ever deleted.** The Notion client has no `DELETE` verb. The most
   destructive operation in the vocabulary is a strike-through.
3. **Every change is exactly reversible, and the reverse is computed before the change
   runs.**
4. **A contradiction is never resolved.** At any setting. The most the system will do is
   write the disagreement down next to the line it disagrees with.
5. **The agent cannot raise its own permissions.** No tool takes an argument that could.
6. **There is exactly one code path that writes**, and an architectural contract fails the
   build if a second appears.

---

## 1. Two switches, both of which must be on

```bash
PALIMPSEST_APPLY=0          # default. Nothing is written, at all.
PALIMPSEST_AUTONOMY=none    # none | low | medium | full | everything
```

They are independent on purpose. `apply` is "may this process write at all"; `autonomy` is
"how much may it write without asking". Setting autonomy to `everything` with `apply=0`
writes nothing, and that combination is tested.

| Autonomy | Applies without review |
|---|---|
| `none` | nothing — every change waits for you |
| `low` | `new`, `corroborates` |
| `medium` | adds `refines`, `supersedes`, `duplicate`, `extends` |
| `full` | every tier that is automatable at all |
| `everything` | adds recording a contradiction (see §3) |

The ladder is derived by subtraction — `RISK_TIERS - NEVER_AUTOMATIC` — rather than
listed by hand, so a new risk tier cannot be automatable by having been forgotten.

> **Tests.** `test_writes_off_holds_everything_regardless_of_autonomy` runs the whole
> autonomy matrix with `apply=0` and asserts nothing reaches the workspace.
> `test_only_the_top_level_admits_the_contradiction_tier` asserts the table above,
> including that inventing a level (`high`, `all`, `yolo`, `max`) is rejected at
> validation rather than silently treated as permissive.

## 2. How sure it has to be, and why that is not one number

A well-calibrated model reports *lower* confidence for "this needs a page of its own" than
for "this agrees with line 12" — the first is a judgement about a whole knowledge base,
the second a comparison of two sentences. A single flat threshold therefore rejects the
**safest** operation most often, while passing a strike-through on the same number, which
is precisely backwards.

So the bar scales with what being wrong would cost:

| The edit would… | Confidence required |
|---|---|
| add a block (`new`, `corroborates`) | 0.60 |
| rewrite a sentence (`refines`, `duplicate`, `extends`, `supersedes`) | 0.75 |
| change what an existing sentence means (`contradicts`) | 0.90 |

`PALIMPSEST_MIN_CONFIDENCE` moves the whole scale; raising it tightens everything.
Anything below its bar becomes a review item, not a silent drop.

## 3. A contradiction is never resolved

This is the property the rest of the design exists to protect. A knowledge base that
silently replaces a true claim with a false one is **strictly worse than no automation**,
because you stop knowing which parts to trust.

So the system does not decide which of two sourced claims is correct. Not at `everything`,
not at any setting, not with any prompt.

What the top rung adds is *recording* the disagreement: one appended callout, directly
beneath the line it argues with, carrying the competing claim and both sources. The
existing sentence is not edited, struck, or archived. The whole thing inverts by removing
one block, so a reader who disagrees with the machine loses nothing by undoing it.

"May record the argument" and "may settle the argument" are different powers. Only the
first is on offer.

> **Tests.** `test_a_contradiction_never_applies_at_any_setting` runs the full matrix.
> `test_recording_a_contradiction_never_edits_the_sentence_it_disagrees_with` asserts the
> plan for a contradiction contains exactly one append and no edit to the existing block.
> `tests/demo/test_promises.py::test_a_contradiction_is_never_applied_even_here` checks
> the same thing against a live model that really does decide a real page is wrong.

## 4. Nothing is deleted

There is no `DELETE` verb on the Notion client. The vocabulary is:

`add_citation` · `update_text` · `insert_footnote` · `strike_block` · `archive_block` ·
`create_page` · `link_pages` · `move_page` · `rename_page` · `set_icon` · `set_cover` ·
`rewrite_section`

`strike_block` leaves the words on the page with a line through them. `archive_block`
moves a block to the trash, which is restorable and which undo restores from a snapshot.

`rewrite_section` is the one operation that is not small: it replaces a run of blocks
wholesale, because a knowledge base worth reading needs sections restructured and no
amount of sentence-level editing produces that. It trades reviewability-at-a-glance and
keeps recoverability — every block it replaces is snapshotted first, so undo restores them
exactly and in order. Those are two separate properties and only the first is given up.

> **Tests.** `test_the_writing_tools_are_a_closed_list` asserts the set of tools that can
> write, by name rather than by count, so adding one is a deliberate act that fails the
> build until the list is updated. `test_a_rewrite_snapshots_what_it_replaces` pins the
> snapshot.

## 5. Every change is reversible, and the reverse is written first

Notion has no transactions. A patch interrupted at operation six leaves five applied, so
each operation's inverse is computed and stored **before** it runs, not after.

Undo rebuilds a replaced section from its snapshot rather than un-trashing the original
blocks, because Notion returns a restored block to the *end* of the page — un-trashing
would hand back the right paragraphs in the wrong order. Costing new block ids to keep
reading order is the trade a reader would choose.

The applier stops at the first failure with status `partial` rather than ploughing on.

> **Tests.** `tests/unit/test_apply.py` runs the whole applier against a fake workspace
> that really appends, really archives and really restores, so a wrong inverse fails there
> exactly as it would against a live workspace. The markdown backend deliberately
> reproduces Notion's awkward behaviours — restore-to-end, "can't edit an archived block",
> append reporting nested descendants — so that a bug in the inverse logic cannot hide
> behind a backend that is nicer than the real one.

## 6. The agent cannot escalate

The agent is a tool-using loop. Its permissions come from the environment, and there is no
tool argument through which it could ask for more: no `autonomy`, `apply`, `force`,
`auto_apply`, `permission`, `override`, `bypass`, `skip_review` or `skip_approval` in any
tool's schema.

Content it reads — a web page, a PDF, a Notion block — is data, not instructions. The
system prompt says so, and the prompt's stated limits are themselves asserted.

> **Tests.** `test_no_tool_schema_can_touch_autonomy_or_apply` walks every registered
> tool's input schema and fails if any of those names appears.

## 7. One write door

Every mutation goes through `palimpsest.notion.apply`. This is not a convention — it is an
[import-linter](https://import-linter.readthedocs.io/) contract, and the build fails if a
planner, the retriever, or the sweeps so much as import the applier, the journal, or the
approval gate.

The whole safety story — every change reversible, every change provenanced, every change
logged — rests on there being exactly one path that writes. A second one would not be a
bug in a feature; it would invalidate the document you are reading.

> **Tests.** Contract 2 in `pyproject.toml`, checked by `lint-imports` in CI.

---

## What this document does not claim

- **That the classifier is always right.** It is not. That is why everything is
  reversible, why every edit carries its reasoning on the page, and why
  `palimpsest eval component` exists to measure your model before you trust it.
- **That a small local model is safe to give write access to.** Measured, several are not
  — one missed every contradiction in the golden set. `palimpsest status` refuses to be
  quiet about an unmeasured or failing model.
- **That your data cannot leave your machine.** It goes wherever your configured model
  provider is. If that matters, `PALIMPSEST_MODEL_PROVIDER=ollama` keeps everything local,
  including embeddings.
- **That there are no bugs.** Several serious ones are recorded in the git history, each
  with the test that now prevents it. The claim is about what the design permits, not
  about the absence of mistakes.

## Reporting something

If you find a way to make this violate any invariant above, that is a security issue, not
a feature request. Please report it privately through
[GitHub security advisories](https://github.com/itsskofficial/palimpsest/security/advisories/new)
rather than as a public issue, and include the reproduction.
