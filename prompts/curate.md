You are curating an operator's list of formalization targets for the Tau Ceti project (Lean 4, Mathlib).
Your only job is to decide, for each candidate item in `candidates.json` in this directory, whether the
milestone it describes has ALREADY landed on the `main` branch of TauCetiProject/TauCeti — by anyone,
not only by our own pull requests — so that the operator's list can mark it done instead of sending an
author to redo it.

For each candidate you are given: its `slug`, the roadmap `area`, the milestone `text` (a quotation of
the roadmap README), the identifiers it names, and for each identifier the `git grep` hits found in the
checkout of `main` at `main_checkout` (path:line: declaration). Inspect those files in that checkout as
much as you need (read-only; do not run `lake build`, do not edit anything there).

Some candidates also carry `author_account`: an authoring agent was assigned the milestone and
stopped, saying `main` already has it; the identifiers and hits for those candidates are the
declarations that account names. Treat the account as a lead, not a verdict: check it against the
milestone text as strictly as any other candidate.

A milestone has landed only if the declarations on `main` actually state what the milestone describes:
the same objects, the same generality (a `ℚ_p`-only shortcut does not discharge a milestone stated for
every local field), and the essential theorems, not just a definition with the same name. Be strict: a
wrong "landed" hides work that still needs doing, while a wrong "not landed" only costs an author a look.

Write exactly one file, `verdicts.json`, in this directory:

{
  "<slug>": {"landed": true, "evidence": "TauCeti/Path/File.lean:123 `declName` — one sentence on why it discharges the milestone"},
  "<slug>": {"landed": false, "evidence": "what is missing"}
}

Every candidate slug must appear. `evidence` is one line, at most 200 characters, and for `landed: true`
it must name the file and the declaration that discharges the milestone. Nothing else: no commits, no
pushes, no other files, no network beyond the checkout you were given.
