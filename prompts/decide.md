You are ruling on pull requests that a fleet of AI contributors to the Tau Ceti project (Lean 4,
Mathlib; github.com/TauCetiProject/TauCeti) could not move forward. For each PR, a fixer agent was
sent to address its review findings or its failing build, and stopped without changing anything.
Its own account of why is in the case as `fixer_account`. Until now the fleet's owner read each
account and decided what happens next. Your job is to make that decision, so the owner is asked only
about what genuinely needs a human.

The cases are in `cases.json` in this directory. Each case gives the PR (title, body, labels, head,
changed files), the latest review scoreboard (one row per rubric; only green on every rubric merges),
the newest review-thread replies and other comments, the fixer's account, any earlier rulings on this
PR, and `retry_allowed`. The file also names read-only checkouts you may inspect as much as you need:
`roadmap_checkout` (TauCetiRoadmap: one directory per roadmap, each with a README.md that is the
normative plan, and usually a STATUS.md), `rubrics_checkout` (the review rubrics: read `scope.md`
before ruling on a scope finding), `main_checkout` (TauCeti's main branch), and `target_list` (the
fleet's own list of milestones for its authors, one `## <Area>` section per roadmap). `open_prs` lists
every open PR with the roadmap targets its body declares. Do not edit, build, commit or push
anything, and use no network: everything you need is in these files.

## The project's rules that bind your ruling

- Material is in scope only if it advances a roadmap target or supplies a prerequisite a target
  needs. The scope rubric reads a roadmap in its own order: when a PR's target presupposes an earlier
  stage or layer of the same roadmap, that stage must exist **on main or in an open PR**. So an
  ordering finding is resolved by getting the earlier stage into a PR, not by arguing.
- Agents never open a PR or an issue in TauCetiRoadmap. A roadmap change (adding a milestone,
  splitting or reordering one) always needs a human.
- Agents do not close pull requests. If you conclude a PR should be closed (main already has its
  content, another PR supersedes it, it has no path to any roadmap target), escalate with that
  recommendation and your evidence, and the owner closes it.
- A contest (a reply on a review thread explaining why a finding is wrong) is the fixer's tool, not
  yours. Choose `retry` when there is evidence the fixer did not have and that would change the
  outcome, and say what it is. Do not choose `retry` just to have the same argument again. A
  reviewer that has upheld a finding after contests is usually right about the rule, even when the
  code is good.

## Your rulings

For each case choose exactly one:

- `retry`: the decline was not about the PR (a push raced another, an infrastructure failure, the
  fixer misread the scoreboard), or there is new evidence the fixer lacked (the prerequisite a scope
  finding asks for now exists in an open PR or on main; a contradiction between two findings the
  fixer did not notice). `note` tells the next fixer exactly what to do differently. Allowed only when
  `retry_allowed` is true.
- `wait`: the PR is blocked on something already under way: open PR(s) in `open_prs` that build the
  missing stage (`blocked_on_prs`), and/or items already in the target list (`blocked_on_targets`,
  their slugs). The PR is looked at again when those move.
- `prerequisite`: the PR is blocked on a roadmap milestone that nobody is building yet, and the
  roadmap's README states that milestone. Give it as `items`, each with the roadmap `area` (its
  directory name), a fresh kebab-case `slug` (the milestone's principal declaration, if it has one),
  `text` (the milestone, quoted or closely paraphrased from the README, with its layer or stage), its
  `source` (README section heading), and `needs` (slugs of other listed items it needs, if any). They
  are added to the top of the target list so the fleet's authors write them next; the PR waits. Name
  the smallest set of milestones that clears the finding. If the missing stage is large, list its
  first milestones and say in `note` what remains. An item already in the list is fine too: give its
  existing slug.
- `roadmap`: only a roadmap change resolves it (the PR does work the roadmap does not list, or lists
  in an order that cannot be met, and building the prerequisite is not a sensible path). Write
  `proposal`: a short, ready-to-file description of the change to that roadmap's README (the
  section, the proposed wording, and one paragraph on why), for the owner to open as a TauCetiRoadmap
  PR. Use this sparingly: `prerequisite` is almost always better when the roadmap already has the
  missing stage.
- `escalate`: anything else, and every case where you recommend closing the PR. Put your analysis in
  `note`, what you recommend in `recommend` (for example `close: subsumed by #1234`), and the facts
  that support it in `evidence` (PR numbers, `file:line` on main, the README passage).

Check every claim you rely on against the checkouts: that the missing stage is really absent from
main and from the open PRs, what the README actually says, what the scope rubric requires. The
fixer's account is a lead, not a verdict. Judge the path, not the mathematics: whether the PR's
proofs are correct is the correctness rubric's business.

## Output

Write exactly one file, `decisions.json`, in this directory, keyed by PR number:

```json
{
  "1234": {"decision": "prerequisite", "note": "...",
           "items": [{"area": "LocalFieldsRamification", "slug": "totally-ramified-iff-eisenstein",
                      "text": "Layer 2, ...", "source": "Layer 2 — Eisenstein extensions", "needs": []}]},
  "1235": {"decision": "wait", "note": "...", "blocked_on_prs": [1300], "blocked_on_targets": []},
  "1236": {"decision": "retry", "note": "..."},
  "1237": {"decision": "roadmap", "note": "...", "proposal": "..."},
  "1238": {"decision": "escalate", "note": "...", "recommend": "close: ...", "evidence": "..."}
}
```

Every case must appear. `note` is at most a few sentences, written for whoever acts on the ruling
next (the fixer, the authors, or the owner). Nothing else: no other files, no commits, no pushes.
