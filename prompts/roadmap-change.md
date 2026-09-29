You are preparing a change to one roadmap in TauCetiRoadmap (github.com/TauCetiProject/TauCetiRoadmap),
the human-curated set of plans that the Tau Ceti Lean library is built from. A pull request in Tau Ceti
is blocked because the roadmap, as written, does not allow it (usually its scope rubric reads the
roadmap's order strictly), and a proposal for how the roadmap should change has been drafted. The
fleet's owner will read your change before deciding whether to file it as a pull request.

`request.json` in this directory holds the Tau Ceti PR (`tauceti_pr`, `tauceti_pr_url`), the drafted
`proposal`, the `ruling` that explains why a roadmap change is needed, and the Tau Ceti fixer's own
account of the block. The TauCetiRoadmap checkout is `repo/`, already on a fresh branch.

Before editing, read `repo/README.md` in full (its **Writing a roadmap** section is the checklist every
roadmap change is held to) and `repo/CONTRIBUTING.md`. Then read the roadmap the proposal names:
`repo/TauCetiRoadmap/<Area>/README.md`, its `Suggested.lean`, and its `STATUS.md` if it has one.

## The change

Edit that roadmap's `README.md`, and its `Suggested.lean` when the change needs it. Nothing else.

- **README.md.** Make the change the proposal describes, in the README's own voice and format: its
  numbering, its dependency-order table, its cross-references. Keep the change as small as the
  proposal allows, and keep everything else in the file as it is. If the proposal is wrong in a
  detail (an item number, a section name, a dependency the README does not actually state), follow
  the README and do what the proposal means.
- **Suggested.lean.** It holds suggested Lean signatures for the README's milestones. It is aids,
  never an exhaustive checklist, and the README stays definitive. Change it when the README change
  adds, splits, renames or restates a milestone that the file prototypes, or when a new milestone
  should have a suggested form. For example, when an item splits into a topological part and a
  holomorphic part, give the topological part its own signature. Leave it alone for a change that
  only moves a dependency. Follow the root README's rules for these files: `sorry` honestly (omit a
  condition you cannot state rather than name an empty one), import the individual `TauCeti.*`
  modules an earlier target already implements rather than restating them, and keep the file's
  opening note. If you change it, build it from `repo/` the way TauCetiRoadmap's CI does, and fix it
  until it builds (the cache settings are already in your environment):

  ```
  lake exe cache get
  lake cache get --package=TauCeti --service=tauceti-public --repo=TauCetiProject/TauCeti --mappings-only --max-revs=20
  lake build TauCetiRoadmap.<Area>.Suggested
  ```

  The first build fetches Mathlib and the Tau Ceti modules it imports, which takes a while; run it
  in the foreground and wait. The fleet builds it again before anything is filed.

If, having read the README, you find the proposal should not be made at all, make no edit and
explain why in `pr.json`'s body; the owner will see that nothing changed.

Do not edit any other file: not STATUS.md or PROGRESS.md (generated), not the issue templates, not
the list of roadmaps, not another roadmap. Do not commit, push, or open anything. Use no network
except the build's own downloads.

## `pr.json`

Write `pr.json` in this directory (not in `repo/`):

```json
{"title": "...", "body": "..."}
```

- `title`: short and specific, naming the roadmap, e.g. `AlgebraicTopology: let the Stage 5 product maps depend on Stage 2 only`.
- `body`: Markdown. Open with a paragraph saying what the change does and why, in plain terms a
  roadmap reviewer can check against the README. Say which Tau Ceti PR it unblocks (its URL) and
  what that PR's scope finding said. If you changed Suggested.lean, say what and why. Say what, if
  anything, the change asks of other roadmaps. Close
  with a line attributing AI assistance, as CONTRIBUTING.md asks: the change was drafted by the Tau
  Ceti fleet's decide stage and written by Claude (name the model you are), and the owner read it
  before filing. No headings are needed for a change this size.
