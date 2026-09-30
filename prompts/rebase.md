You are reconciling the branch with current main on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The branch either conflicts with current `main`, or the merge sweep handed off an update it cannot perform on this contributor-owned fork, or the merge queue failed on this head and the sweep flagged it. A mergeable fork may still need this update; do not stop just because Git reports no textual conflict. Bring it up to date with `main` and resolve the conflicts so it can merge again. Work autonomously to completion.

## Rebase onto current main
- Fetch and integrate the latest `main`:
  ```
  git fetch origin
  git merge origin/main      # (or: git rebase origin/main — either is fine; merge is simpler to resolve)
  ```
- Resolve every conflict on its merits:
  - **`TauCeti.lean` (the intentionally empty root module)**: preserve `main`'s version; do not add imports or reconstruct it.
  - **A source file under `TauCeti/`**: resolve so both the upstream change and your PR's intent are preserved. If `main` now provides something your PR duplicated, prefer the upstream version and drop the duplicate.
- Do NOT discard upstream work to "win" a conflict, and do NOT weaken or delete your PR's real content to dodge one. If a conflict is genuinely irreconcilable (your PR's target no longer makes sense because `main` subsumed it), stop and say so in your report rather than forcing a merge. In that case name what subsumed it: find the upstream change with `git log --oneline origin/main -- <each file this PR touches>` (squash-merged PRs end their subject with `(#NNNN)`), and end your report with one line `Subsumed-by: #NNNN` (several numbers separated by spaces; `Subsumed-by: unknown` only if `git log` shows nothing). A human decides whether to close this PR, and that line is what they act on.

**When the merge queue failed on this head** (a `Merge-queue recovery` comment on the PR saying the queue evicted it repeatedly): the PR's own build was green, and the queue's build, which merges it onto the `main` of that moment, was not. The usual cause is that `main` moved or renamed something this branch uses after its last merge: a module split into a directory (`Foo.lean` became `Foo/Basic.lean`, so `import …Foo` is a bad import), or a renamed or removed declaration. Git then merges cleanly, and the build is what fails. Read the queue's failure (`gh run list --repo TauCetiProject/TauCeti --event merge_group` for the `gh-readonly-queue/main/pr-__PR__-…` runs, and `gh run view <id> --log-failed`), merge `main`, and fix what it names: point the import at the module that now declares what this file uses, or use the new name. The comment may say the branch "already includes current `main`"; check with `git rev-list --count HEAD..origin/main` rather than believing it.

If the branch already includes current `main` and no concrete repair is needed, report that no update is needed and stop. Do not manufacture a commit or push an empty change just to satisfy the submission instructions.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT hand-edit the root `TauCeti.lean` — it stays intentionally empty (see above). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Everything under `namespace TauCeti`.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, say so in your report and stop.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter.

Merging upstream workflow or pin changes as part of bringing in `main` is expected. Do not author independent changes to those human-owned files. The sweep request is bound to the old head; after a successful push it no longer schedules rebase work. Do not remove the request label yourself or reset any attempt counter.

## Verify before pushing (all MUST pass, after the merge/rebase)
```
lake exe cache get
lake cache get --service tauceti-public --repo TauCetiProject/TauCeti || true   # TauCeti's own artifacts for the modules `main` changed since your branch's base (Lake backtracks to the newest revision the service holds, so a fresh merge commit is fine); without this, `lake build` recompiles all of them from source
lake build
lake exe axioms
lake exe module-system
git fetch -q origin main; base_ref="$(git merge-base origin/main HEAD)"
{ git diff --name-only --diff-filter=d "$base_ref" -- 'TauCeti/*.lean'; git ls-files --others --exclude-standard -- 'TauCeti/*.lean'; } \
  | sort -u | sed 's/\.lean$//; s#/#.#g' > "${TMPDIR:-/tmp}/lint-modules.txt"
LINT_ONLY_MODULES="${TMPDIR:-/tmp}/lint-modules.txt" bash scripts/lint-env.sh   # as CI runs it: only the modules this branch changes
```
Iterate until green. Never push red — a botched conflict resolution that builds red is worse than the conflict. CI's `build` check is all of these, not `lake build` alone. `lint-env` runs here as CI runs it, on the modules this branch changes; a violation only in declarations outside the diff is lint debt on `main`, not this PR's to fix.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. When a repair is needed, do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the merge/resolution (if `git merge` left a merge commit, keep its default message; otherwise `<type>: <subject>`, ending the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  It compare-and-swaps the PR branch against the head you started from (so it works whether you merged or rebased, and never clobbers a concurrent push). Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report; do not work around it.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which files conflicted, how you resolved each, and the exact `lake build` / `lake exe axioms` / `lint-env` result lines proving green + axiom-clean. Do not claim green unless you saw it.
