# The GitHub gate

Every GitHub operation the worker fleet makes — the worker's own `gh` calls, its git
fetches and pushes, the claim leases, the review engine, the progress tool, and
whatever an agent does with `gh` or `git` on its PATH — passes through one shared
boundary before it is dispatched, and reports back after. The boundary is a
directory of small files under one `flock`, not a daemon: seven workers, the fleet
view, an agent's shim and an interactive shell all share one allowance, and a
restart neither resets nor doubles it, because the budgets are rolling windows of
timestamps and the cooldown and the halt are files.

This document is the operator's reference for the implementation: part 1 (the
store, the admission rules, the wiring, the CLI, the agent boundary, and the offline
harness) and part 2 (the publication ledger and its reconciliation, the fleet-shared
read cache, and the interaction contract). The design it implements is
`handoffs/GITHUB_GATE_DESIGN.md`; the brief whose acceptance tests it runs is
`TAUCETI_GITHUB_SAFETY_IMPLEMENTATION_BRIEF.md`.

## Architecture

```
 worker process                     agent subprocess (host mode)
 ──────────────                     ────────────────────────────
 gh_run()  ─┐                       PATH: scripts/shim/{gh,git} → scripts/ → …
 run_claim_sh ├─ admit ─┐            gh shim ── reads ───────── admit ─┐
 gated_git   │         │            gh shim ── pr create ── gh-safe-pr-create ── admit ─┤
 review/     │         │            git shim ── fetch/clone ─────── admit ─┤
 progress  ─┘          │            git shim ── push ── git-safe-push ── admit ─┤
                       │            claim.sh ─────────────────────── admit ─┤
                       │            git credential helper (tauceti-gate credential) ─┤
                       ▼                                                          ▼
              ┌──────────────────────── $TAUCETI_GATE_DIR (flock) ────────────────────────┐
              │ state.json   RUNNING | COOLDOWN | HALTED_MANUAL, until, reason, login pin │
              │ budget.json  rolling windows per kind and per push repo; in-flight registry │
              │ quarantine.json   {"<op>:<target>": {reason, until?}}                      │
              │ halt.json    the identity gate's record shape; presence = HALTED_MANUAL     │
              │ events.log   JSONL telemetry (admit / refuse / record / transition …)       │
              │ disabled     marker: every admit refused (offline / `tauceti-gate disable`) │
              │ spawns.log   real gh spawns by the shim, for `report`'s UNINSTRUMENTED check│
              │ publications/<id>.json   the publication ledger (design §6, below)          │
              │ cache/review_state/      the fleet-shared scoreboard sidecars (design §5)    │
              │ cache/inflight/<pr>.<tag>.json   a miss in flight: peers wait, not refetch   │
              │ cache/reactions/         the contest claim with TAUCETI_REACTIONS=0          │
              │ incidents/<kind>-<key>.json   what was NOT sent to GitHub, and why           │
              └───────────────────────────────────────────────────────────────────────────┘
```

In process, `tauceti_worker.gate.Gate.admit(op, target, kind)` returns an `Admission`
or raises `GateRefused(reason, until)`; `Gate.record(admission, outcome)` releases the
slot and classifies the outcome. Shell callers use `tauceti-gate admit`/`record`
(`scripts/tauceti-gate`, also `tauceti gate …`). An admission handed to a child
script travels as `TAUCETI_GATE_TOKEN` so the child neither admits again nor counts
twice; a `TAUCETI_GATE_TOKEN` never crosses into an agent.

### Kinds and what is accounted

| kind | what | accounted as |
| --- | --- | --- |
| `api_read` | any `gh` read: `pr list/view/checks`, `issue list`, `api` GET, `api graphql` queries, `repo list`, `rate_limit`; `gh pr checkout`; `tauceti-progress due` | one per dispatch, per page, per retry |
| `api_mutation` | `api -X POST/PATCH/PUT/DELETE`, a field argument, a GraphQL mutation, `pr create/edit/comment/review/close`, `issue create/edit`, `repo fork`, `workflow run`; the review engine (op `review`, weight 1); `tauceti-progress apply` | one per dispatch |
| `git_read` | clone, fetch, ls-remote, pull; `claim.sh holds/read/list`; a credential request with no admission behind it | one per claim.sh subcommand (which may be 1–3 git transport calls) |
| `git_push` | `git-safe-push`; `claim.sh acquire/renew/release/gc`; the review outbox sync (op `sync`) | one per dispatch, windowed **per repository** |
| `identity` | the one `gh api user --jq .login` of the identity gate | exempt from the budgets; not from a halt, cooldown, or store error |

Retries and pagination inside `gh_run` are admitted and recorded one by one: the
accounting is of dispatches, not of calls the caller thought it made.

## Admission

In order. The first that applies decides; the reason is what `GateRefused.reason`,
`tauceti-gate admit`'s stderr and `events.log` carry.

| # | check | refusal reason |
| --- | --- | --- |
| 1 | `halt.json` present, or `state.json` is `HALTED_MANUAL` | `halted` |
| 2 | `COOLDOWN` and `now < until` (an expired cooldown flips back to `RUNNING` here) | `cooldown` (+ `until`) |
| 3 | the `disabled` marker exists | `disabled` |
| 4 | `TAUCETI_IDENTITY_OK` is set and differs from the login pinned in `state.json` (the first validated login pins it) | `login` |
| 5 | `(op, target)` is quarantined (permanently for a permission denial, or until a time for a transient run) | `quarantined` (+ `until`) |
| 6 | target allowlist — API reads: `TauCetiProject/*`, `kim-em/TauCetiWorker`, `TAUCETI_FORK`, the claims namespace (`CLAIM_REPO` / `TAUCETI_CLAIM_REPO` / `TauCetiProject/tauceti-claims`), account endpoints (`-`), and reads of the validated login's own namespace; API writes: the same without the own-namespace extension; pushes: the fork, the claims namespace, the round's `TAUCETI_PUSH_REMOTE` (op `push` only), `TauCetiProject/TauCetiData` for op `sync`; canonical is never a claim target, whatever `CLAIM_REPO` says | `target` |
| 7 | in flight on the lane (`api` or `git`) ≥ `max_inflight` — waited out up to `admit_wait`, polling with the lock released; stale entries (owner pid dead, or past `inflight_deadline`) are reaped first | `busy` |
| 8 | `api_mutation`/`api_read` with no per-hour cap configured | `unconfigured` |
| 9 | mutation spacing (`last_mutation_at + 5 s`) — waited out if it fits in `admit_wait` | `spacing` (+ `until`) |
| 10 | rolling windows: mutations per minute and per hour; reads per hour minus the reserve (ops `preflight`, `reconcile`, `rate_limit` may use the reserve); pushes per minute and per hour **per repository**; git reads per hour — a per-minute cap is waited out if it fits, an hourly one refused at once | `budget` (+ `until`) |
| — | the store cannot be read or written (missing directory permissions, corrupt JSON, a lock that cannot be taken) | `store-error` |
| — | `TAUCETI_GATE_REQUIRED=1` with `TAUCETI_GATE_DIR` unset | a hard error (`Die`) at construction, never a bypass |

A refusal stays with the caller and takes its existing failure path: `gh_run`
returns a synthetic `CompletedProcess` with rc 75 and `gate: refused (<reason>)` on
stderr (the `GateRefused` rides along as `p.gate_refused`); `run_claim_sh` returns 2
("could not be registered"); the scripts exit 75 (`gh-safe-pr-create`,
`git-safe-push`) or 2 (`claim.sh`); the review, progress and sync sites decline
through `NoProgress`. Nothing retries a refusal. The loop preflight probes the store
before spending anything: `HALTED_MANUAL` exits 77 like the identity halt, a
cooldown is slept out to its end (no back-off escalation: the end is known), a store
error is reported locally and retried after `TAUCETI_POLL` seconds without contacting
GitHub.

## Recording and transitions

`record` releases the in-flight slot, appends the outcome, and classifies it. The
HTTP status is parsed from gh's stderr (`HTTP 4xx`) when gh echoed one; `Retry-After`,
`x-ratelimit-remaining/reset` and `x-github-request-id` likewise, or from headers the
caller passes. When no status is present the text alone is classified.

| outcome | transition |
| --- | --- |
| success, or a failure that is none of the below (a lost CAS race, a bad argument) | none; the op's transient count and the secondary-limit run are reset by a success |
| 429; `Retry-After`; "secondary rate limit" / "abuse detection" | `COOLDOWN`: `Retry-After` if given, else `x-ratelimit-reset` when the primary bucket is 0, else 60 s doubling per consecutive hit, bounded to 15 min |
| "API rate limit exceeded" (primary) | `COOLDOWN` to `x-ratelimit-reset` when echoed, else the doubling rule |
| 401, "Bad credentials", "not logged in" | `HALTED_MANUAL`, reason `invalid-credentials`, `halt.json` written |
| 403 with "suspended", "locked", "too many" | `HALTED_MANUAL`, reason `account-halt` |
| 403 with "Resource not accessible", "Must have push access", "Write access … not granted", "protected branch"; git "remote rejected … permission", "Permission to … denied" | quarantine `(op, target)`, no `until` — lifted only by `tauceti-gate revalidate` |
| any other 403 | `COOLDOWN` 15 min, reason `unclassified-403`, for investigation |
| 5xx, "unexpected end of JSON input", a reset or timed-out connection | transient; after 3 consecutive on one op, quarantine that `(op, target)` for 30 min |

While halted, any other write still in flight is appended to `events.log` as
`uncertain`; the publication ledger (below) is what reconciles it. GraphQL `errors` bodies, partial pages and
truncated JSON are not a gate concern: `GitHub.open_prs` raises and the survey marks
itself `github_failed`, which the round treats as "do not act", never as "no work".

Clearing: a cooldown expires on its own, or `tauceti-gate resume` clears it after
printing the incident. `HALTED_MANUAL` clears **only** through
`tauceti work [--worker-id <id>] --clear-halt`, which prints the per-worker record
and the fleet store's record and then returns the store to `RUNNING`; restarting a
worker or the fleet never clears it. A quarantine clears with
`tauceti-gate revalidate <op> <target>` once the permission or configuration has
been fixed.

## The CLI

```
tauceti-gate admit <kind> <op> <target> [--weight N] [--no-wait]   token on stdout; 75 + "gate: refused (<reason>)" on refusal
tauceti-gate record <token> <status> [--detail TEXT] [--detail-file F] [--header K=V]… [--duration-ms N]
tauceti-gate status [--json]        JSON or a one-line summary; works with no network, no store yet, or an unreadable store
tauceti-gate report [--since 1h]    peaks per minute/hour by kind, totals by kind and op, pushes per repo, refusals by reason,
                                    cooldowns/halts, quarantines, uncertain writes, UNINSTRUMENTED shim spawns
tauceti-gate halt <reason> [detail…] / resume        resume prints the incident and clears a cooldown only
tauceti-gate enable / disable       the `disabled` marker: every admit refused (the offline mode's off switch)
tauceti-gate revalidate <op> <target>
tauceti-gate credential get         a git credential helper (see the boundary below)
tauceti-gate publication create --kind author|fix|rebase [--branch B --head-sha S --pr N] [--repo R] [--remote URL]
tauceti-gate publication begin <id> <step> [--sha S] [--branch B] [--body-file F]   0 proceed / 75 refused / 3 duplicate
tauceti-gate publication end <id> <step> ok|fail [--remote-id X] [--detail-file F]  prints the state written
tauceti-gate publication show <id> / list
tauceti-gate reconcile [<id> | --all]   resolve uncertain steps with one read each; exit 1 if any stays uncertain
```

`status` and `report` also print the publication queue: how many publications are in
progress, and each parked one with its reason. Both read local files only — the
fleet view never touches GitHub (T12).

`<status>` is an HTTP status, `ok`, `fail`, or `rc:<n>`. `python -m tauceti_worker gate …`
and `tauceti gate …` are the same program. `scripts/tauceti-gate` is the wrapper the
agent PATH and the scripts use; it runs the worker's own interpreter (`TAUCETI_PYTHON`,
exported by the worker) so the package imports.

## Environment

All durations in seconds, all counts per rolling window. "Scope" says who reads it.

| variable | default | scope | meaning |
| --- | --- | --- | --- |
| `TAUCETI_GATE_DIR` | _(unset = gate disabled, unless required)_ | worker, scripts, shims, CLI | The store directory. The fleet wrapper sets it (`~/claude/gq2-fleet/gate`); one directory per account. |
| `TAUCETI_GATE_REQUIRED` | _(unset)_ | same | `1` makes a missing `TAUCETI_GATE_DIR` a hard error rather than the no-op gate. Set it in every fleet. |
| `TAUCETI_GATE_MUTATIONS_PER_HOUR` | **none — owner decision** (design §9 proposes 40) | worker, CLI | Rolling hourly cap on `api_mutation`. Unset: every mutation is refused `unconfigured`. |
| `TAUCETI_GATE_READS_PER_HOUR` | **none — owner decision** (design §9 proposes 600) | worker, CLI | Rolling hourly cap on `api_read`. Unset: every read is refused `unconfigured`. |
| `TAUCETI_GATE_MUTATIONS_PER_MINUTE` | `20` | worker, CLI | Rolling per-minute cap on mutations. |
| `TAUCETI_GATE_READS_PER_MINUTE` | `100` | worker, CLI | Rolling per-minute cap on API reads; a cold survey (~210 per-PR reads) spreads over about two minutes instead of bursting in one. |
| `TAUCETI_GATE_MUTATION_SPACING` | `5` | worker, CLI | Minimum gap between two mutations (brief §4.3). |
| `TAUCETI_GATE_READS_RESERVE` | `200` | worker, CLI | Part of the hourly reads cap only the reserved ops (`preflight`, `reconcile`, `rate_limit`) may consume. |
| `TAUCETI_GATE_PUSHES_PER_MINUTE` / `_PER_HOUR` | `4` / `60` | worker, CLI | Per-repository push caps (GitHub's guidance is 6/min/repo; the claims namespace is shared). |
| `TAUCETI_GATE_GIT_READS_PER_HOUR` | `300` | worker, CLI | Hourly cap on git reads. |
| `TAUCETI_GATE_MAX_INFLIGHT_API` / `_GIT` | `1` / `1` | worker, CLI | In-flight per lane (brief: one in-flight API request for the pilot). |
| `TAUCETI_GATE_ADMIT_WAIT` | `30` | worker, CLI | How long `admit` waits for a busy slot, the spacing, or a per-minute window before refusing. |
| `TAUCETI_GATE_INFLIGHT_DEADLINE` | `900` | worker, CLI | An in-flight entry older than this is reaped (as is one whose pid is dead). |
| `TAUCETI_GATE_COOLDOWN_BASE` / `_MAX` | `60` / `900` | worker, CLI | The secondary-limit doubling: first hit, and the bound. |
| `TAUCETI_GATE_UNCLASSIFIED_403_COOLDOWN` | `900` | worker, CLI | The investigation pause for a 403 that is neither a rate limit, a halt, nor a known denial. |
| `TAUCETI_GATE_TRANSIENT_MAX` / `_QUARANTINE` | `3` / `1800` | worker, CLI | Consecutive 5xx on one op before it is quarantined, and for how long. |
| `TAUCETI_GATE_CALLER` | _(unset)_ | any client | A label for `events.log`'s `caller` (default: the worker id). |
| `TAUCETI_GATE_TOKEN` | _(internal)_ | scripts, credential helper | An admission the parent made for this exact call; the child reuses it and does not record. Never exported into an agent. |
| `TAUCETI_GIT_OP` | _(internal)_ | credential helper | `git_read` or `git_push`: the kind a credential request belongs to; set by the wrappers and the git shim. |
| `TAUCETI_REAL_GH` / `TAUCETI_REAL_GIT` | _(set by the worker)_ | shims, scripts | Absolute paths of the real binaries, resolved before the shim directory is prepended. |
| `TAUCETI_PYTHON` | _(set by the worker)_ | `scripts/tauceti-gate` | The interpreter to run the gate CLI with. |
| `TAUCETI_GATE_CLI` | `scripts/tauceti-gate` | scripts, shims | Override the CLI the shell side calls (tests). |
| `TAUCETI_PUBLICATION_ID` | _(internal)_ | scripts, shim | The round's publication (design §6): created by the worker before the agent launches and exported into the agent env; the wrapper scripts record their steps against it. Unset (an operator running a script by hand, or no gate): the scripts run unrecorded, as before. |
| `TAUCETI_STUCK_ISSUES` | `1` | worker | `0` keeps the "Review stuck" tracking issue off GitHub: the escalation is an incident file under `incidents/` and gh is not called (brief §8.1). |
| `TAUCETI_REACTIONS` | `1` | worker | `0` keeps the contest claim (the 👀 on the contesting reply) off GitHub: a marker under `cache/reactions/` carries it for every worker on this store, and add/remove/age make no gh call. |
| `TAUCETI_CONTEST_MAX_EXCHANGES` | `2` | worker | Automated contest re-reviews per PR head. At the cap the round posts nothing (no claim, no engine) and writes a `contest-cap` incident: a human is needed. |
| `TAUCETI_FAKE_SCENARIO` / `TAUCETI_FAKE_LOG` | _(unset)_ | `tests/fakes/*` | The offline harness's scenario file and invocation log. |

The two per-hour caps are the ones the brief says must be configured rather than
defaulted: nothing here supplies a value for them, and the fleet wrapper has to.

## Publications (design §6)

Only two write shapes leave the fleet through the wrapper scripts, and each is a
**publication**: a record under `publications/<id>.json` with one entry per step, so a
crash, a lost response or a moved PR head never becomes a second copy of the same write.

| kind | steps | who creates it |
| --- | --- | --- |
| `author` | `push` → `pr_create` → `marker_check` | `do_roadmap`, before the agent launches; the branch is the agent's to name and `git-safe-push` records it |
| `fix` (also fix-ci and bump) | `push` → `comment` | `_do_fixlike`, after the checkout, on the head it checked out |
| `rebase` | `push` → `comment` | `_do_fixlike` |

The record: `{id, kind, pr, repo, remote, branch, head_sha, steps: [{name, state,
remote_id, at, detail, sha?, digest?, pid?}], worker, pid, created_at, parked}`. The
worker exports the id as `TAUCETI_PUBLICATION_ID` into the agent's environment (and
into the bubble env list); the scripts are the publisher's steps:

- `git-safe-push` marks `push` **sent** (with the local tip) before git runs and
  **done** with that tip after a successful push.
- `gh-safe-pr-create` appends `<!--tauceti-publication:v1 {"id":"<id>"}-->` to the
  body (the body file is rewritten; a `--body` string is extended), marks `pr_create`
  **sent** before gh runs and **done** with the PR number parsed from gh's output;
  `marker_check` is closed at the same time, since the target marker was verified
  locally before the create.
- the `gh` shim's one admitted write (the `fix.md` reply on a review thread) appends
  the same marker to the `body=` field and records the `comment` step the same way,
  **done** with the comment id from gh's JSON.

A step is **done** only when the response was parsed and the remote id stored. A
success whose response cannot be parsed (empty stdout, a timeout after the request
went out) is **uncertain**; a failure is **uncertain** too unless its text proves
nothing landed (a `[rejected]`/stale-info push, a 4xx that is not "already exists"),
in which case the step returns to **pending**. Any uncertain step parks the
publication `needs-reconciliation`.

**Refusals** (`publication: refused (<reason>)` on stderr, exit 75, nothing sent):
`step-state` — the step is sent, uncertain or done, and a step is never resent;
`parked` — the publication needs reconciliation; `stale-head` and `head-unreadable`
(below); `unknown-step`. A `duplicate` reply exits 3 from the CLI and the shim turns it
into exit 0 with nothing posted.

**Revalidation before every step.** The lease first (`claim.sh holds`, then `renew`,
in the scripts as before — T10). Then, for a fix/rebase, ONE admitted `gh pr view
--json headRefOid` (op `preflight`, on the reads reserve): the live head must equal
the publication's `head_sha` — or, once our own `push` is done, the tip that push
left — else the step is refused `stale-head`, the publication is parked `stale-head`,
and the round yields (T09). A head that cannot be read refuses too: fail closed.

**Reconciliation.** `tauceti-gate reconcile <id>` (or `--all`) resolves each
uncertain step with ONE admitted read (op `reconcile`, reserved): the branch tip via
`git ls-remote` against the recorded local tip; the PR via `gh pr list --head
<branch>` matched on the hidden publication marker in its body (`marker_check` is
settled from the same body, no extra read); the reply via the PR's review comments
matched on the marker. Found → **done** with the remote id. Not found, several found,
or the read failed → the step stays **uncertain**, the publication stays parked, and
`status`/`report` show it. Nothing is ever resent automatically (T07).

**On round start** (`run_round`, before the survey, not under `--dry-run`): every
publication of this worker whose sender is gone has its `sent` steps marked
`uncertain` and is reconciled before any new work. A publication with nothing
uncertain but steps still pending — the round died between a done `push` and the
`pr_create` — is parked `interrupted`: the worker does not resume it (the agent that
would write the PR body is gone; the next authoring round starts a new branch), but a
publisher run again under the same id resumes at the pending step and the done ones
refuse (T08). The `interrupted` mark clears when a step is resumed.

**Digests.** The `comment` step records the sanitised digest of the reply (markers
stripped, whitespace collapsed, lower-cased). `begin comment` refuses `duplicate` when
another publication on the same PR and head already has a done comment with that
digest, so a restarted fix round that reaches the same conclusion posts nothing (T14).

Bubble mode: the id crosses into the container with the push-arbiter env, but the
gate store does not (as in part 1), so steps taken inside a bubble are not recorded
and the round's summary line shows them pending. The pilot recommendation stands
either way: run gated rounds on the host or point the container at the store.

## The shared read cache (design §5)

With a gate store, `ReviewState`'s sidecars (`<pr>.key.json`, `<pr>.contest.json`,
`<pr>.json`) live under `$TAUCETI_GATE_DIR/cache/review_state/` instead of the
per-worker `state/<id>/cache/scoreboard/`. Upstream's `updatedAt` keying, the
30-minute backstop and the forced live re-read of the one chosen candidate are
untouched: the files are keyed by PR and `updatedAt`, so any worker's read entitles
every worker, and `assumed` is still not `fresh`.

Misses are coalesced across processes. A reader about to fetch a PR's comments (or
review comments) writes `cache/inflight/<pr>.comments.json` (`.contest.json`) —
`{pid, at}` — under the gate lock; a second reader finding a live marker (pid alive,
younger than 20 s) polls every 0.5 s for up to 20 s and then re-reads the sidecar
instead of fetching. A marker whose owner died or aged out is taken over. A forced
read never waits (it must be live) but claims the marker when it is free, so peers
coalesce onto it. If the first reader's fetch failed, the waiter finds nothing
entitled and fetches for itself — coalescing never turns one failure into two
silences (T12).

## The interaction contract (brief §8.1)

Three toggles keep nonessential writes off during the pilot; what they suppress is
recorded locally under `incidents/` (one file per condition, refreshed in place with a
count), and `tauceti gate status` and `report` do not need GitHub to show it.

- **Stuck-review issues** (`TAUCETI_STUCK_ISSUES`, default `1`). `ensure_stuck_issue`
  already dedups by exact title and skips the edit when the body is unchanged (T14
  proves both against the fake). At `0` it writes `incidents/review-stuck-<pr>.json`
  and never calls gh.
- **Reactions** (`TAUCETI_REACTIONS`, default `1`). At `0` the contest claim is a
  marker under `cache/reactions/`, read by the same `fresh_claim_age` the survey and
  the dispatch re-check use, so the contest path's bookkeeping works unchanged and no
  add/remove cycle reaches GitHub. The marker is shared by every worker on the store,
  not across hosts.
- **Contest exchanges** (`TAUCETI_CONTEST_MAX_EXCHANGES`, default `2`). A new counter
  `review-contest-<pr>-head-<sha12>` counts automated re-reviews per head beside the
  existing per-PR and per-rubric lifetime counters; the survey suppresses a contest at
  the cap and `do_review` refuses it before the claim and the engine, writing
  `incidents/contest-cap-<pr>-<sha12>.json` ("needs a human") and yielding.

## The agent boundary (host mode)

`host_agent_argv` builds the agent's environment behind the boundary the design
calls for (§4), for every host agent, gate or no gate:

- `PATH` leads with `scripts/shim` (a `gh` and a `git`), then `scripts`; the real
  binaries are recorded in `TAUCETI_REAL_GH`/`TAUCETI_REAL_GIT` for the shims and the
  wrappers, which call those and so never recurse into the shims.
- `GH_TOKEN`, `GITHUB_TOKEN`, `CLAIMS_TOKEN` and `TAUCETI_GATE_TOKEN` are dropped.
- `GH_CONFIG_DIR` is a per-round read-only copy of the operator's gh config holding
  `config.yml` and `hosts.yml` only (`chmod a-w`), so `gh auth …` has nowhere to
  persist anything. `GH_NO_UPDATE_NOTIFIER=1` keeps gh from wanting to write.
- `GIT_CONFIG_GLOBAL` is a per-round copy of `~/.gitconfig` whose
  `credential.helper` for `https://github.com` (and gist) is `!tauceti-gate
  credential`, with `useHttpPath` on so the helper sees the repository. The helper
  admits a `git_read`/`git_push` (kind from `TAUCETI_GIT_OP`, default `git_read`)
  unless the caller already holds an admission (`TAUCETI_GATE_TOKEN`), then
  delegates to the real gh's `auth git-credential`; refused, it prints nothing and
  exits 1, so a raw `git push` by absolute path fails cleanly for want of a
  credential.

The `gh` shim allows `pr list|view|checks|diff|status`, `issue list|view`, `run
view|list`, `search`, and `api` GET (no `-X`/`--method`/`-f`/`-F`/`--input`), each
admitted as `api_read`; routes `pr create` to `gh-safe-pr-create`; admits the one
write a prompt asks for (a `POST` reply on a review thread of a canonical PR:
`repos/TauCetiProject/TauCeti/pulls/<n>/comments/<id>/replies`) as `api_mutation`;
and refuses `auth *`, `repo *`, `workflow *`, `api graphql`, every other `api`
method and every other `pr`/`issue`/`run` write with exit 75 and the sanctioned
wrapper named. The `git` shim passes local commands through; `push` is refused
unless the round set `TAUCETI_PUSH_REF`, and then only `HEAD` to that ref, through
`git-safe-push`; `remote add|set-url` is refused for anything but
`https://github.com/…`; `fetch|pull|ls-remote|clone` are admitted as `git_read`.

### Remaining bypasses — pilot blockers

Environment scrubbing and a PATH shim are not a credential boundary on a macOS host
with the operator's login Keychain and SSH keys, and the brief says to name what is
left rather than pretend. Still open in host mode:

1. **The login Keychain.** `security find-generic-password` (or any process
   reading the operator's `gh` Keychain item) yields the token to whoever asks. The
   worker itself reads only Claude's item; an agent can read gh's.
2. **`curl`/`urllib` with such a token**, or with any token the agent finds on
   disk: there is no network policy on the host.
3. **SSH.** `~/.ssh/config` names the `github-claude` key. The git shim refuses SSH
   remotes by name, and nothing in the worker uses SSH, but an agent that runs the
   real git by absolute path with an `ssh://` URL uses whatever key the config
   selects.
4. **The checkout's own scripts** (`Scripts/` in the TauCeti checkout) can perform
   administrative writes with a token from 1 or 2.

Mitigation available now, and the recommendation for the pilot: run it in bubble
mode (`sandbox = "bubble"` in the fleet wrapper; egress denied, the auth proxy
scoped to the allowlisted repositories), or as a separate macOS user with no
Keychain access and no SSH keys. Part 2 does not close these; a deployment-level
isolation check does.

## Offline mode and the harness

`tauceti work --offline` (design §8) puts `tests/fakes` first on `PATH`, points a
required gate store at `state/<id>/gate-offline`, sets the two per-hour caps if they
are unset (40 / 600), and refuses to start if `gh` on `PATH` is not the fake or if
`GH_TOKEN`/`GITHUB_TOKEN` is set. It is a dry run in the brief's sense: zero remote
requests, and a failed fake never falls back to the real service (an unmatched `gh`
fails; an unmatched `git` runs the real git with every `https://github.com/…` URL
rewritten to a local bare repository or to a path that does not exist).

The fakes (`tests/fakes/gh`, `git`, `curl`, `security`) answer from
`TAUCETI_FAKE_SCENARIO` (JSON: ordered entries per binary with an argv-prefix
`match` and/or a `contains` substring, `rc`, `stdout`, `stderr`, `sleep`, `times`;
`bare_repos` maps `owner/repo` to a local bare path for pushes) and append every
invocation to `TAUCETI_FAKE_LOG`. `curl` and `security` are never answered: they
log and fail.

### Acceptance tests (brief §8.3)

All in `tests/gate_t*.py`, registered in `tests/run-all`, offline, proven by mock
unless noted. Run them with `env -u CLAIM_REPO tests/run-all`.

| id | file | what it proves |
| --- | --- | --- |
| T01 | `gate_t01_shared_allowance.py` | 7 workers + a dashboard + an interactive admit in 9 processes: never more than one in flight on the API lane, the per-minute mutation cap holds across all, a restarted process sees the same counters, a dead owner's slot is reaped |
| T02 | `gate_t02_credential_halt.py` | one 401 halts the fleet: heartbeat renew, cleanup release, escalation issue, reads, the shim and a fresh process are all refused `halted`; `resume` does not clear it; the identity gate refuses to start; an identity halt halts the store; 403+suspended halts too |
| T03 | `gate_t03_canonical_claim.py` | canonical in any spelling and a key outside `refs/tauceti-claims/` are refused by claim.sh before git; the seam and the gate refuse canonical pushes (`target`); the fork, the claims namespace and the round's head repo pass; the git shim refuses a push the round was not given |
| T04 | `gate_t04_403_classes.py` | "Must have push access" quarantines one (op, target) and nothing else; a bare 403 is a 15-minute global pause refusing reads, renews and the rate_limit probe; a permission-rejected claim push quarantines that namespace; none loops |
| T05 | `gate_t05_rate_limits.py` | `Retry-After: 7` → cooldown 7 s, no admits (rate_limit included) before it; `x-ratelimit-remaining: 0` + reset → cooldown to the reset; secondary text → 60, 120, 240 s, bounded at 15 min, reset by a success; `gh_run` sleeps a refused retry out rather than spinning; every retry is admitted and recorded |
| T06 | `gate_t06_restart_cooldown.py` | a cooldown survives a restart and is seen identically by a second controller in another process; after it ends the next mutation is admitted and the one after refused `spacing` (no burst); the per-minute window remembers what came before |
| T07 | `gate_t07_lost_response.py` | the real `gh-safe-pr-create` against a fake gh whose `pr create` answers rc 0 with no output: `pr_create` is uncertain and the publication parked; a second run is refused without spawning gh; `reconcile <id>` finds the PR by its hidden marker with one `pr list` and marks it (and `marker_check`) done; a publication whose PR the fake does not know stays uncertain through `reconcile --all` from a fresh process, and its create stays refused; a create that dies with a connection error is uncertain and reconciles; a 422 returns the step to pending |
| T08 | `gate_t08_crash_between_steps.py` | the real `git-safe-push` pushes a real commit into a local bare fork and records the tip; nothing else runs; `reconcile --all` from a new process parks the publication `interrupted`; a second push under the id is refused; `gh-safe-pr-create` under the same id resumes at `pr_create` and completes it; the fake git log holds exactly one push. A push whose sender died mid-step (`sent`, dead pid) is marked uncertain, resolved by ONE `ls-remote` (tip equal → done; absent → uncertain, parked, refused) |
| T09 | `gate_t09_stale_head.py` | a fix publication on head A; the fake `pr view` says B: `git-safe-push` is refused `stale-head` after one read, nothing is pushed, the shim's reply is refused too, the publication is parked; with the head unchanged the push proceeds and the reply is then checked against the tip OUR push left and posts once with the marker; an unreadable head refuses (fail closed) |
| T10 | `gate_t10_lease_lost.py` | a stand-in claim.sh: lost and not renewable → exit 1, no push, no head read, the step never sent; re-acquired → one fresh head read, one push; expired-but-renewable → renew then push; a held lease on a moved head is still refused `stale-head` |
| T11 | `gate_t11_partial_data.py` | truncated JSON, a GraphQL `errors` body and endless pagination are `github_failed`, never "no work", and leave the store RUNNING; three 502s quarantine the op for 30 min after a bounded retry, and the next survey is refused without spawning gh |
| T12 | `gate_t12_coalesced_misses.py` | two worker processes miss the same PR's comments at once (the fake gh takes 1.5 s): exactly one fetch, one `fresh` and one `assumed` answer holding the same board, the sidecar under the fleet store; `status`, `status --json` and `report` make zero gh/git calls; a dead owner's marker is taken over without the 20 s wait; a peer whose fetch failed (a 404) leaves the waiter to fetch for itself |
| T13 | `gate_t13_identity_override.py` | `GH_TOKEN` in front of `TAUCETI_EXPECT_LOGIN` halts with `identity-mismatch` naming the source and halts the store; the login pin refuses a different login; the agent env carries no token and a read-only gh config; the shims refuse `gh auth *` and SSH/other-host remotes without spawning; the credential helper obeys the gate |
| T14 | `gate_t14_noop_suppression.py` | the shim's reply with the same sanitised content on the same head is skipped (exit 0, no POST) and a different one posts; `TAUCETI_REACTIONS=0`: add/age/remove with zero gh calls and the claim still visible to the survey's check; the contest cap: `do_review` at 2 exchanges yields before the 👀 and the engine, with zero gh calls and one refreshed incident file; `TAUCETI_STUCK_ISSUES=0`: an incident file and no gh; on: an unchanged body is one `issue list` and no edit. `do_review` is the real function on a stubbed worker; the rest is the real entry point |
| T15 | `gate_t15_offline_escape.py` | under the offline environment: `gh api -X POST` and `gh auth token` via the shim, `curl`, a raw `git push` by absolute path, and a push credential request all fail without a credential or a remote; `security` is never invoked; `Claims.release` goes through the gate (admitted against a local bare repo; refused when halted); `tauceti work --offline --dry-run` refuses with `GH_TOKEN` set or a non-fake gh, and otherwise runs a whole round whose fake log holds reads only. The last item exercises the real worker entry point, not a stub |
| T16 | `gate_t16_store_unavailable.py` | a corrupt `budget.json` or an unreadable directory: every remote admit refused `store-error` (gh_run rc 75, claim rc 2, the preflight probe raises), no gh/git spawned, `status` prints the error, local claim locks still work, repair restores admission; `TAUCETI_GATE_REQUIRED=1` without a directory is a hard error |

What is proven by mock versus exercised in the worker environment: T07–T10 drive the
real wrapper scripts and shim (the exact programs an agent runs) against the fakes
and a local bare repository, with the publication created through the real CLI; the
worker's own creation of the publication before an agent launch and the round-start
reconcile are wired in `work_units.py` but not driven end to end here (a host round
needs a model). T12 runs the real `ReviewState` in two processes. T14's contest cap
calls the real `do_review` on a stubbed worker. None of the six reaches the live
worker environment; a deployment-level check (bubble mode, the sandbox egress test)
is still the pilot's own.

## Where the design was not followed exactly

- **Reads of the operator's own namespace** (`<login>/*`) are allowlisted for
  `api_read` in addition to the design's list: fork resolution asks
  `repos/<me>/<name> --jq .fork` before it knows the fork's name. Writes get the
  design's list only.
- **`TauCetiProject/TauCetiData`** is a push target for op `sync` only: the review
  outbox sync (inventory row 39) is a real push path, probed for push permission
  first, and the design's push allowlist did not name it.
- **`ensure_fork()` exports `TAUCETI_FORK`** once it has resolved the fork, so the
  push allowlist, the wrappers and the heartbeat child know it without resolving
  again. An operator-set `TAUCETI_FORK` is honoured as before.
- **A claim.sh subcommand is one accounted `git_read`/`git_push`** even though it
  makes up to three git transport calls (`ls-remote`, `fetch`, `push`). The push is
  the operation GitHub rate-limits per repository; the reads that precede it are
  its preamble.
- **A permission-rejected claim push is rc 2, not rc 1**: claim.sh used to read every
  rejected push as a lost race. A denial is now an error with its git output, so the
  gate can quarantine the namespace instead of every round retrying it.
- **`tauceti-gate revalidate`** is an addition to the CLI the task listed: a
  permission quarantine has no `until`, and the brief wants an operator to
  authorise revalidation explicitly.
- **The `gh` shim admits one write**, the review-thread reply `fix.md` instructs,
  rather than refusing every `api` mutation: without it the fix prompt's reply step
  has no sanctioned path. It is `api_mutation` op `reply`, on canonical PRs only.
- **`gh api graphql` is refused by the shim** outright: no prompt uses it, and a
  document the shim cannot classify is safer refused than guessed.
- **The credential helper does not double-count**: a wrapper that admitted a push
  hands its token down, and the helper only admits on its own when there is none.

Part 2:

- **The comment step's head check expects our own pushed tip**, not the publication's
  original `head_sha`, once `push` is done. Read literally, "the PR head must equal the
  recorded head_sha" would refuse every reply that follows the fix push it belongs to.
- **`marker_check` has no send of its own.** gh-safe-pr-create verifies the target
  marker locally before the create and closes the step with the PR number; reconcile
  settles it from the body the `pr_create` lookup already fetched. No third read.
- **The PR is found by `gh pr list --head <branch>` and the body marker**, not by
  `--search`: GitHub's search index lags a fresh create by minutes, while the branch
  filter is exact, and the branch was create-only pushed by this publication.
- **An interrupted author publication is not resumed by the worker.** The design says
  "resume or reconcile"; resuming `pr_create` needs the PR body the dead agent was
  going to write. It is parked `interrupted` and resumable under its id by hand (T08).
- **fix-ci and bump are `fix` publications** (the same push + comment shape); the
  design names only fix and rebase.
- **A definite failure returns a step to `pending`** rather than staying uncertain: a
  `[rejected]` CAS push or a 422 create proves nothing landed, and leaving them
  uncertain would park every lost race for an operator to clear.
- **The contest cap has its own per-head counter** beside the existing lifetime
  per-PR and per-rubric ones; "using the existing contest counters" would have
  capped a PR for life at two exchanges however many heads it went through.
- **Steps inside a bubble are unrecorded** (the store does not cross into the
  container, as in part 1). The id is forwarded so a store-aware container could
  record them.
