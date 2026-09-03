# Quota and pacing

The README covers the pacing rule and the controls. This page covers where the
usage numbers come from and how the pacer behaves at the edges.

## Credential sources

The pacer reads the credential files the official CLIs already maintain,
`~/.claude/.credentials.json` and `~/.codex/auth.json`, and queries each
provider's usage endpoint. It honors `$CLAUDE_CONFIG_DIR`, so switching between
a personal and a work Claude account is paced correctly.

Reading quota never spends anything, with the one exception described under
"window bootstrap" below. `tauceti status`, the dashboard, and an auto selection
that lands on Codex make no model request at all.

## Keeping the Claude token alive: `--auto-refresh`

By default the worker never rotates a refresh token. When the Claude access token
expires it reports Claude unavailable, and your next `claude` run renews it. That
is fine at a keyboard and fatal unattended: `work --loop` will sit at
`claude usage HTTP 401 (access token expired or rejected; log in again)` until
someone intervenes.

`tauceti work --loop --auto-refresh` (or `$TAUCETI_AUTO_REFRESH=1`) lets the
worker renew the token itself once it is within 90 minutes of expiry. The same
flag covers Codex: the operator's `~/.codex/auth.json` (a ~10-day access token
that nothing on the host otherwise rotated) is renewed once it is within
`TAUCETI_CODEX_REFRESH_SKEW` (default 48 h) of expiry, under the same host
flock and the same caveat below — no copy of that refresh token may exist
anywhere else. Without it every Codex worker reads `codex token expired; refresh
left to the operator` until a human runs `codex`.

**Only turn it on when nothing else uses that credential file.** Claude and Codex
issue single-use refresh tokens: exchanging one retires it and returns a
replacement. TauCeti serializes its own processes on the host, but it cannot
serialize an interactive `claude` sharing `~/.claude/.credentials.json`, a second
refresher, or a copy of the credential on another machine — a rotation here logs
any of those out. The shape this is meant for is a worker running as its own
user, with its own Claude account nobody signs into interactively;
`$CLAUDE_CONFIG_DIR` gives the same separation on a shared login. On macOS the
Claude half of the flag does nothing: the Keychain is the store, and the section
below applies instead. The Codex half works everywhere.

When it is on: it renews the file the operator owns, never a worker's stripped
mirror, and it never touches a credential carrying no refresh token, so the
Docker deployment's dedicated refresher stays the single writer there. A
worker's copy never holds one: the first seed at `--isolate-home` goes through
the same stripping mirror as every later re-mirror. Rotations are rate-limited
by markers beside the credential, shared across every worker on the host. Only
the paths about to run something renew — the loop pacing towards a round, a
round resolving the model it will launch, and the launch stage. Reading
commands stay reads: `tauceti status` and the dashboard report an expired token
rather than rotating it behind you.

## macOS and the login Keychain

On macOS, Claude Code keeps its credentials in the login Keychain rather than in
a file: service `Claude Code-credentials` for the default config dir, and
`Claude Code-credentials-<suffix>` for a non-default `$CLAUDE_CONFIG_DIR`
(`<suffix>` is the first 8 hex characters of the SHA-256 of the directory path,
exactly as set). An isolated worker therefore has its own item, and the pacer
reads that item first, falling back to the operator's un-suffixed one only for
a worker whose item does not exist yet. It used to read only the operator's
item, so every worker parked when the operator's token expired and ran on when
its own had.

**Refresh tokens are single-use, one chain per login, and copies are fatal.**
Exchanging a refresh token retires it; a second holder of the same token fails
its next refresh, and Claude Code then wipes that holder's item. So no worker
ever holds one. The worker's own Keychain item is an access-token mirror of the
operator's: whenever the operator's access token changes, the worker's item is
rewritten with it and an empty `refreshToken`, the way `mirror_creds` keeps a
stripped file copy elsewhere. The operator's un-suffixed item is never written
by TauCeti, and a worker's `claude` can never refresh — an access token that
expires mid-round fails that round, which is what the 90-minute pre-launch
renewal is there to prevent. Bubble's private Keychain handoff seeds the
container from the worker's item, so the in-container `claude` cannot refresh
either.

Renewal is Claude Code's alone. With `TAUCETI_CLAUDE_WARM=1` (put it in a
worker's `env` table, or export it for `work --loop`), a token within 90 minutes
of expiry — or one the usage endpoint has just rejected — is renewed by a
warm-up run: one `claude -p` Haiku turn, one turn at most, run with
`$CLAUDE_CONFIG_DIR` removed from its environment so it refreshes the
*operator's* item, the one chain on this login. Claude Code notices the expired
token, exchanges the refresh token and writes the Keychain itself; the worker's
item is re-mirrored from it in the same cycle. The run is serialized host-wide
(`~/.cache/tauceti/claude-warm.lock`, so N workers whose shared token lapses
together do not race) and attempted at most once per ten minutes, so it costs
roughly one Haiku call per token lifetime — about every eight hours. Without the
flag the pacer reports Claude unavailable on expiry, as before, and your next
interactive `claude` run is the way back. The file-based `--auto-refresh` never
touches the Keychain.

A locked Keychain, which is what you get headless or over SSH, reports
unavailable with a hint to `security unlock-keychain` first.

Bubble uses the credential file directly where Claude stores credentials in a
file; on macOS it receives the Keychain credential through the private handoff
described in [the sandbox notes](sandbox.md).

## Reading Claude's two windows

Claude's session and weekly windows reset on separate clocks, so they are read
independently and neither is inferred from the other. Each window's raw state is
kept before any pacing is applied.

The structured `limits` array is authoritative for each window. Legacy flat keys
are used only when `limits` omits that window; they cannot override a structured
entry.

A missing window or invalid data, such as an unreadable reset timestamp or
non-numeric usage, stops the provider and reports the specific problem:

```
weekly limit missing from usage response
session reset timestamp invalid
```

rather than a generic "usage unknown". An unreadable constraint is not the same
as no constraint.

## The window bootstrap

There is one gap where the endpoint reports a window with no usage and no reset
clock: right after that window rolls. Only a Claude request can open the new
window, so `tauceti` makes one small `claude -p` turn to do it, drops the cached
usage, and re-reads. The fresh telemetry, not the request, then decides whether a
round runs.

The bootstrap runs only under these conditions:

- It happens at the launch stage of a round that has already found work, so a
  poll that finds nothing to run costs nothing.
- Every other window must be active with real headroom. A window that is at
  budget, over pace, exhausted, missing, or unreadable forbids the bootstrap.
- It respects your pace curve. Under a curve whose budget stays at 0 for the
  first stretch of a window, say `--pace 0:0,90:0,100:95`, a fresh window may not
  be opened at all, and the status says so (`pace budget stays 0% through 90% of
  the window`) rather than quietly opening one to manufacture a clock.
- It is claimed under a lock in a shared ledger beside your credentials *before*
  the request goes out, so every worker on that account, whatever its worker id,
  checkout, or isolated `$HOME`, makes at most one request per window period,
  even if one of them crashes mid-flight.

If the window still is not reporting afterwards, the status reads
`session bootstrap attempted; awaiting fresh usage` and the worker stays parked.

## Why "strictly under"

A provider is available while `used%` is strictly under the budget for the
elapsed fraction of the window. Strictly, because the request being decided
costs something: sitting exactly on the budget
(`session at budget (20% elapsed: used 50% = 50% pace budget)`) is a pause, not a green light.

If usage cannot be read at all, the provider is treated as unavailable rather
than assumed free.
