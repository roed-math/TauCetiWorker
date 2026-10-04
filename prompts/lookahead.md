You are proving a target of the operator's list for TauCetiProject/TauCeti AHEAD of its supplier, on a private branch of the account's fork, `__FORK__/TauCeti`. The target cannot go to Tau Ceti yet: it needs declarations that another list item supplies, and that supplier has not landed on `main`. You prove it now against **stubs** of the supplier's pinned statements, so that when the supplier lands, an author round only has to swap each stub for the landed declaration and open the pull requests the plan below describes. You are in a clean checkout of `main`. Do honest mathematics, and work autonomously to completion.

**This branch is never a pull request.** Do not open a PR or an issue anywhere (the PR wrapper refuses in this round), and never write to TauCetiRoadmap. The round's only write is ONE push of the branch `__BRANCH__`, at the end.

## The target
__ASSIGNED__

The suppliers you stub (each is a list item whose statements its roadmap's `Suggested.lean` pins):
__SUPPLIERS__

__RESUME__

## Read before writing Lean
- The target's roadmap: `__ROADMAP_DIR__/__AREA__/README.md` in full (it is definitive), and the target's pinned forms in `__ROADMAP_DIR__/__AREA__/Suggested.lean`. Then each supplier's README section and its declarations in its own roadmap's `Suggested.lean` (`grep -n '<name>' __ROADMAP_DIR__/<Area>/Suggested.lean`).
- A supplier with an open PR: read that PR's diff (`gh pr diff <N> --repo TauCetiProject/TauCeti`). What it states is what will land; where it differs from `Suggested.lean`, stub the PR's form and record both. Partial PRs of a supplier that already merged are on `main`: use what they landed instead of stubbing it.
- The review rubrics, concatenated at `__RUBRICS__`. The port will be judged against every one of them, so write this proof as you would write the PR (they address reviewers: take their criteria, ignore their roles and output formats).
- Before writing any declaration, `grep` the pinned Mathlib (`.lake/packages/mathlib`, once `lake exe cache get` has run) and `TauCeti/` for it.

## The stubs
Create `TauCeti/Lookahead/__CAMEL__/Stubs.lean`, holding ONLY what the target consumes from the suppliers:
- Each supplier declaration restated against `main`'s real API, under the name and namespace it will have on Tau Ceti (the roadmap's `TauCetiRoadmap.<Area>` namespace becomes Tau Ceti's `TauCeti` namespace, placed the way the neighbouring code on `main` is), with the hypotheses and conclusion of the pinned form. A theorem's body is `sorry`. A definition's body is `sorry` too, and the stub file then states, also with `sorry`, the API the supplier is pinned to provide about it: the proof may use only that API, never the stub definition's body.
- Nothing `main` already has, and nothing the suppliers' roadmaps do not pin.
- The file has the usual copyright header and `module`, then `set_option warningAsError false` (the library builds with warnings as errors and every `sorry` warns), then a module docstring saying these are lookahead stubs for the supplier slugs, to be replaced by the landed declarations.
- Nothing else on the branch may use `sorry` or `admit`, or touch `warningAsError`.

## The proof
- Put the target's declarations where its pull requests will put them: the module paths, namespaces and names its roadmap and the neighbouring code call for, importing the stub module wherever a supplier is needed. Follow every rule a real PR follows: everything under `namespace TauCeti`, the module system (`module`, `public import`), Mathlib naming and docstrings, no `set_option` outside the stub file, at most 1000 lines per new file, nothing outside `TauCeti/` but `LOOKAHEAD.md`. Never downgrade the target to a lookalike: a weakened statement or scaffolding carrying its name is worse than no branch.
- Prove the whole target if you can. Then plan its split into pull requests as the port will open them: each about 200–600 lines and one topic, building on `main` plus the splits before it. Splits that do not import one another can be opened at the same time, so give each split the list of splits that must merge first. **The last split must need every other split, carry the target's exported theorem(s), and be the only one that completes the target.**

## Verify (all of these MUST pass before you push)
```
lake exe cache get
lake build
"__BIN__/lookahead-check"
```
`lake build` must be green: warnings are errors everywhere but the stub file. `lookahead-check` confirms that `sorry`, `admit` and `warningAsError` occur only under `TauCeti/Lookahead/`, that nothing outside `TauCeti/` changed but `LOOKAHEAD.md`, that `LOOKAHEAD.md` opens with a readable header, and that the axiom audit of the changed modules finds no axiom but `sorryAx`. Fix whatever it reports. Run these in the FOREGROUND and wait for each: you are non-interactive, nothing will resume you, and a branch that is not pushed is lost.

## LOOKAHEAD.md
At the repository root (the only file outside `TauCeti/`). Its FIRST line is exactly one machine-readable header, for example:
```
<!--tauceti-lookahead:v1 {"area":"__AREA__","slug":"__SLUG__","main":"<the main commit you built on>","status":"complete","suppliers":["<supplier slug>"],"splits":[{"n":1,"after":[],"title":"<PR subject>"},{"n":2,"after":[1],"title":"<PR subject>"}]}-->
```
`status` is `complete` when the whole target is proved on the branch, else `partial`; `suppliers` are the slugs you stubbed; `splits` is the plan, numbered from 1, each with the splits it needs first (the last split needs all the others). Below the header, in prose:
- for each stubbed declaration: its `Suggested.lean` signature copied verbatim, the stub as you stated it, where it came from (`Suggested.lean`, or PR #N's diff), and every difference between them with the reason;
- for each split: its files, the declarations it adds, the splits it needs first, and the one-line PR subject;
- if `partial`: what is proved and exactly what remains.

## Commit and push, once
Work on the branch `__BRANCH__` (create it with `git checkout -b __BRANCH__` unless you are resuming it). Commit everything, the stubs, the proof and `LOOKAHEAD.md` (message `lookahead: __SLUG__`; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`). Then push with the project's wrapper, and ONLY the wrapper:
```
"__BIN__/git-safe-push" __BRANCH__
```
That is the round's one write: never push an intermediate state, and never run a raw `git push`.

## If the target won't close, or time runs short
The worker ends this round at __DEADLINE__, and a branch that is not pushed by then is lost. On a large target, prove a coherent part first and push it as `partial` well before that; the next session resumes it. Push only a branch that builds and passes `lookahead-check`. If you proved a coherent part, push it with `"status":"partial"` and say exactly what remains. If you could make no real progress (a supplier's pinned statement is too unclear to stub, or the target needs something no listed supplier provides), push nothing and end your report with the line `Lookahead: no branch — <reason>`.

## Report
End with: the target, the stubbed declarations by name and where each statement came from, the split plan (one line per split), the files with their line counts, what your own audit against the rubrics changed, and the line `Lookahead: pushed __BRANCH__ (<status>)`.
