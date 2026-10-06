#!/usr/bin/env python3
"""A Bors merge reads as a merge. Since 2026-10-06 TauCeti merges through Bors, which squashes the PR
onto main and closes it as `[Merged by Bors] - <title>`; GitHub reports that PR CLOSED with no
`mergedAt`, so every `state == "MERGED"` test and every `--state merged` listing missed it (five
lookahead ports re-offered splits that had landed). GitHub.pr_view and GitHub.pr_list report it as
MERGED; a PR closed without merging stays CLOSED; callers get back only the fields they asked for.
No network. Exit 0 = all hold; 1 = a mismatch."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


def done(payload):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


class FakeGH(tc.GitHub):
    def __init__(self, replies):
        super().__init__("TauCetiProject/TauCeti")
        self.replies = list(replies)
        self.calls = []

    def _gh(self, args):
        self.calls.append(args)
        return self.replies.pop(0)


def fields_asked(args):
    return args[args.index("--json") + 1].split(",")


BORS = {"state": "CLOSED", "title": "[Merged by Bors] - feat: a thing", "closedAt": "2026-10-06T02:33:10Z",
        "mergedAt": None, "number": 12244, "body": "b"}
SHUT = {"state": "CLOSED", "title": "feat: abandoned", "closedAt": "2026-10-05T00:00:00Z", "mergedAt": None,
        "number": 12000, "body": "c"}
PLAIN = {"state": "MERGED", "title": "feat: merged the old way", "closedAt": "2026-10-06T01:48:00Z",
         "mergedAt": "2026-10-06T01:48:00Z", "number": 12174, "body": "d"}

# pr_view
gh = FakeGH([done({k: BORS[k] for k in ("state", "title")})])
d = gh.pr_view(12244, ["state"])
check("a Bors merge views as MERGED", d == {"state": "MERGED"}, str(d))
check("pr_view asks for the title it needs", "title" in fields_asked(gh.calls[0]), str(gh.calls[0]))
gh = FakeGH([done({k: BORS[k] for k in ("state", "title", "mergedAt", "closedAt")})])
d = gh.pr_view(12244, ["state", "mergedAt"])
check("a Bors merge's merge time is its close time", d == {"state": "MERGED", "mergedAt": "2026-10-06T02:33:10Z"}, str(d))
gh = FakeGH([done({k: SHUT[k] for k in ("state", "title", "body")})])
d = gh.pr_view(12000, ["state", "title", "body"])
check("a PR closed without merging stays CLOSED", d == {"state": "CLOSED", "title": "feat: abandoned", "body": "c"}, str(d))
gh = FakeGH([done({"body": "x"})])
check("a view without a state asks for nothing extra", gh.pr_view(1, ["body"]) == {"body": "x"}
      and fields_asked(gh.calls[0]) == ["body"])

# pr_list
gh = FakeGH([done([{k: r[k] for k in ("number", "body", "state", "title")} for r in (BORS, SHUT, PLAIN)])])
rows = gh.pr_list(["number", "body"], state="merged", search='"tauceti-target:v1" in:body sort:updated-desc')
args = gh.calls[0]
check("merged PRs are listed from the closed ones, by search",
      args[args.index("--state") + 1] == "closed" and "--search" in args, str(args))
check("the merged listing keeps Bors merges and plain merges, not closures",
      rows == [{"number": 12244, "body": "b"}, {"number": 12174, "body": "d"}], str(rows))
gh = FakeGH([done([])])
gh.pr_list(["number"], state="merged")
check("a merged listing without a search is still a search", "--search" in gh.calls[0], str(gh.calls[0]))
gh = FakeGH([done([{"number": 1}])])
check("an open listing is unchanged", gh.pr_list(["number"], state="open") == [{"number": 1}]
      and gh.calls[0][gh.calls[0].index("--state") + 1] == "open" and "--search" not in gh.calls[0])

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
