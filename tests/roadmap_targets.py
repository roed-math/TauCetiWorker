#!/usr/bin/env python3
"""--roadmap-targets: the operator target list (tauceti_worker.targets) and its effect on do_roadmap.

An operator who wants workers to author only PRs on the path to one goal writes a markdown list of
milestones grouped by roadmap area. This harness pins, without GitHub or a model:

  1. The parser: statuses, `needs` (backtick slugs, `none`, absent), the preamble, `## Gaps…`
     sections skipped, a malformed `- [` line ignored rather than fatal, a missing marker -> Die.
  2. `open_areas` and `render_area_block` (prerequisite statuses resolve across areas; `[?]` for a
     slug the file never defines).
  3. Selection in `do_roadmap`, driven the way tests/fork_authoring.py does: the auto pick lands in
     an area with open targets and never lists areas over the network; --roadmap-skip removes an
     area; a pinned area with no open item is NoProgress; the rendered block reaches the prompt on
     both the bubble and host paths; with the env unset the prompt says `none` and nothing changes.
  4. The live overlay: an open PR's target marker puts a listed item in flight, a merged PR's marks
     it done, an unknown `needs` slug is warned about and treated as done, and a GitHubError on the
     merged fetch falls back to the file's marks. Eligibility = open and every need done.
  5. The worker-side claim loop, with claim.sh scripted: rc [1, 0] takes the second candidate and
     sets the push-arbiter env; all-1 is NoProgress; rc 2 proceeds unclaimed; the acquire cap holds;
     the prompt assigns the chosen slug; areas are visited in random order.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import targets as T

S = sys.modules["tauceti_worker.survey"]  # the package exports survey() the function under that name

FORK = "alice/TauCeti"
fails = 0
ST: dict = {}  # scripted claim.sh verdicts, acquires made, log lines, heartbeat/cleanup registrations


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


SAMPLE = """\
# GQ2 axiom targets
<!-- tauceti-targets:v1 -->
Goal: discharge the nine GQ2 axioms.

Items are ordered; take the first open one whose needs are done.

## Sheaves
- [ ] `sheaf-pullback-exact` — pullback of sheaves is exact (serves: B10; needs: `site-basics`, `stalk-functor`)
- [~] `stalk-functor` — the stalk functor on a site (serves: B3c; needs: none; in flight: #5504)
- [x] `site-basics` — sites and sieves (serves: B1; done: #5513)
> a note line, ignored
- [ ] this line has no slug and must be ignored with a warning
- [ ] `sheaf-hom` — internal hom of sheaves (serves: B4; needs: `site-basics`)

## Cohomology
- [ ]   `derived-pushforward`   —   derived pushforward along a morphism (serves: B7; needs: `sheaf-pullback-exact`, `ghost-slug`)
- [ ] `no-needs-clause` — an item without a needs clause (serves: B2)
- [X] `uppercase-done` — done, uppercase X (serves: B2; needs: none)

## AllDone
- [x] `finished` — everything here is done (serves: B0)
- [~] `moving` — and the rest is in flight (serves: B0; needs: `finished`)

## Gaps
- [ ] `gap-item` — sections titled Gaps are skipped entirely (serves: nothing)
"""


# ---- 1. parser ------------------------------------------------------------------------------------
def test_parser():
    t = T.parse_targets(SAMPLE)
    check("parser: areas in file order, Gaps skipped", list(t.areas) == ["Sheaves", "Cohomology", "AllDone"])
    check("parser: gap item is nowhere", t.find("gap-item") is None)
    sh = t.areas["Sheaves"]
    check(
        "parser: malformed item line ignored",
        [it.slug for it in sh] == ["sheaf-pullback-exact", "stalk-functor", "site-basics", "sheaf-hom"],
    )
    check("parser: statuses", [it.status for it in sh] == ["open", "inflight", "done", "open"])
    check("parser: markers", [it.marker for it in sh] == ["[ ]", "[~]", "[x]", "[ ]"])
    check("parser: needs as backtick slugs", sh[0].needs == ["site-basics", "stalk-functor"])
    check("parser: needs none -> []", sh[1].needs == [])
    check("parser: text stops before the metadata", sh[0].text == "pullback of sheaves is exact")
    check("parser: other metadata kept verbatim", sh[0].meta == ["serves: B10"])
    check("parser: in-flight metadata kept", sh[1].meta == ["serves: B3c", "in flight: #5504"])
    check("parser: whole line kept", sh[2].line.startswith("- [x] `site-basics`") and "done: #5513" in sh[2].line)
    check("parser: area recorded on the item", all(it.area == "Sheaves" for it in sh))
    co = t.areas["Cohomology"]
    check(
        "parser: lenient whitespace",
        co[0].slug == "derived-pushforward" and co[0].text == "derived pushforward along a morphism",
    )
    check("parser: absent needs -> []", co[1].needs == [] and co[1].meta == ["serves: B2"])
    check("parser: uppercase X is done", co[2].status == "done")
    check(
        "parser: preamble captured without title/marker",
        t.preamble
        == "Goal: discharge the nine GQ2 axioms.\n\nItems are ordered; take the first open one whose needs are done.",
    )
    try:
        T.parse_targets("# no marker\n\n## Sheaves\n- [ ] `x` — y\n")
        check("parser: missing marker -> Die", False)
    except tc.Die as e:
        check("parser: missing marker -> Die", "tauceti-targets:v1" in str(e))
    # spaced hyphen and a spaced-out marker are accepted
    t2 = T.parse_targets("<!--tauceti-targets:v1-->\n## A\n- [ ] `s` - text here (needs: `q`)\n")
    check(
        "parser: spaced marker and hyphen separator",
        t2.areas["A"][0].text == "text here" and t2.areas["A"][0].needs == ["q"],
    )
    check("parser: empty preamble", t2.preamble == "")


# ---- 2. open_areas / render_area_block -------------------------------------------------------------
def test_helpers():
    t = T.parse_targets(SAMPLE)
    check("open_areas: only areas with a [ ] item, file order", T.open_areas(t) == ["Sheaves", "Cohomology"])
    block = T.render_area_block(t, "Cohomology")
    check("render: preamble leads", block.startswith("Goal: discharge the nine GQ2 axioms."))
    check("render: header counts", "Targets in `Cohomology` (2 open of 3):" in block)
    check(
        "render: needs resolve across areas, unknown is [?]",
        "- [ ] `derived-pushforward` — derived pushforward along a morphism (serves: B7; needs: sheaf-pullback-exact [ ], ghost-slug [?])"
        in block,
    )
    check(
        "render: absent needs renders none",
        "- [ ] `no-needs-clause` — an item without a needs clause (serves: B2; needs: none)" in block,
    )
    check("render: done item keeps its marker", "- [x] `uppercase-done` —" in block)
    sh = T.render_area_block(t, "Sheaves")
    check("render: mixed statuses resolve", "needs: site-basics [x], stalk-functor [~]" in sh)
    check("render: in-flight metadata survives", "(serves: B3c; in flight: #5504; needs: none)" in sh)
    check(
        "render: file order",
        sh.index("`sheaf-pullback-exact`") < sh.index("`stalk-functor`") < sh.index("`site-basics`"),
    )
    check("render: unknown area", "(no targets listed for this area)" in T.render_area_block(t, "Nowhere"))


# ---- 3. selection in do_roadmap --------------------------------------------------------------------
class FakeGitHub:
    """`pr_list` only: the merged bodies the live overlay reads, or a GitHubError when `fail` is set."""

    def __init__(self, merged_bodies=(), fail=False):
        self.merged_bodies = list(merged_bodies)
        self.fail = fail
        self.calls = []

    def pr_list(self, fields, *, author=None, state="open"):
        self.calls.append((tuple(fields), state))
        if self.fail:
            raise tc.github.GitHubError("gh pr list failed: 504")
        return [{"number": 9000 + n, "body": body} for n, body in enumerate(self.merged_bodies)]


def _marker(area, slug):
    return f'<!--tauceti-target:v1 {{"focus":"{area}","id":"{slug}"}}-->'


def _survey(*open_markers):
    """A minimal Survey-shaped object: `open_prs` carrying PRInfo built the way the real survey does."""
    prs = []
    for n, (area, slug) in enumerate(open_markers):
        prs.append(S.PRInfo.from_json({"number": 100 + n, "body": f"This PR …\n{_marker(area, slug)}\n"}))
    return types.SimpleNamespace(open_prs=prs)


def _stub_round(tmp):
    """The same stubbing as tests/fork_authoring.py::test_roadmap, minus the source material, plus a
    real Claims whose claim.sh verdicts are scripted (ST["script"], default 0) and whose heartbeat is
    recorded instead of spawned."""
    os.environ["TAUCETI_RESPECT_CLAIMS"] = "false"
    os.environ.pop("TAUCETI_PUSH_EXPECT", None)
    tc.work_units.ensure_fork = lambda: FORK
    tc.work_units.administrative_hold_avoid_list = lambda *_args: "none"

    def fake_fetch_ref(repo, dest):
        if repo == tc.constants.REVIEW:
            (Path(dest) / "rubrics").mkdir(parents=True, exist_ok=True)
            (Path(dest) / "rubrics" / "_common.md").write_text("SHARED PROTOCOL\n")
            (Path(dest) / "rubrics" / "api-design.md").write_text("ANGLE api-design\n")
        return True

    tc.work_units.fetch_ref = fake_fetch_ref
    cap = {}
    tc.work_units.run_in_bubble = lambda w, target, prompt, opts, **k: (
        cap.update(target=target, prompt=prompt, **k) or 0
    )
    tc.work_units.prepare_checkout = lambda cfg: True
    tc.work_units.run_agent_host = lambda cwd, prompt, model, logdir: cap.update(host_prompt=prompt) or 0

    def no_network(gh):
        raise AssertionError("roadmap_areas must not be consulted under a target list")

    tc.work_units.roadmap_areas = no_network
    ST.clear()
    ST.update(acquires=[], script=[], log=[], cleanups=[])

    def fake_claim_sh(args, claim_repo):
        ST["acquires"].append((tuple(args), claim_repo))
        return ST["script"].pop(0) if ST["script"] else 0

    tc.round.run_claim_sh = fake_claim_sh
    tc.round.claims_repo = lambda: "alice/tauceti-claims"
    tc.round.Claims.start_heartbeat = lambda self, key, repo: ST.__setitem__("heartbeat", (key, repo))
    real_log = tc.config.log

    def capture_log(msg):
        ST["log"].append(msg)
        real_log(msg)

    tc.work_units.log = capture_log
    tc.round.log = capture_log
    tc.targets.log = capture_log
    cfg = types.SimpleNamespace(state=tmp, wid="worker3", checkout=tmp / "checkout", logdir=tmp / "logs")
    ctx = types.SimpleNamespace(add_cleanup=lambda fn: ST["cleanups"].append(fn))
    w = types.SimpleNamespace(cfg=cfg, gh=None, claims=tc.round.Claims(cfg, ctx))
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
    return w, opts, cap


def _run(w, opts, reason, bubble=True, sv=None):
    w.claims.held = None
    os.environ.pop("TAUCETI_CLAIM_KEY", None)
    os.environ.pop("TAUCETI_CLAIM_REPO", None)
    return tc.work_units.do_roadmap(w, sv, types.SimpleNamespace(reason=reason, pr=0, head=""), opts, bubble=bubble)


def _first_key():
    return ST["acquires"][0][0][1] if ST["acquires"] else None


def test_selection():
    tmp = Path(tempfile.mkdtemp(prefix="targets-test-"))
    tfile = tmp / "gq2-targets.md"
    tfile.write_text(SAMPLE)
    w, opts, cap = _stub_round(tmp)
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)
    os.environ.pop("TAUCETI_ROADMAP_SKIP", None)

    # (a) auto pick lands in an open-target area; roadmap_areas (network) is never called.
    picked = set()
    for _ in range(12):
        cap.clear()
        _run(w, opts, "auto")
        picked.add(cap["prompt"].split("Start with `", 1)[1].split("`", 1)[0])
    check("select: auto pick lands only in open-target areas", picked and picked <= {"Sheaves", "Cohomology"})
    check("select: auto pick never lands in an all-done area", "AllDone" not in picked)
    # "any" (--roadmap-only "") is restricted the same way.
    cap.clear()
    _run(w, opts, "any")
    check("select: any is restricted to open-target areas", "Start with `AllDone`" not in cap["prompt"])

    # (b) skip removes an area from the pick.
    os.environ["TAUCETI_ROADMAP_SKIP"] = "Sheaves"
    picked = set()
    for _ in range(8):
        cap.clear()
        _run(w, opts, "auto")
        picked.add(cap["prompt"].split("Start with `", 1)[1].split("`", 1)[0])
    check("select: skip removes an area", picked == {"Cohomology"})
    os.environ["TAUCETI_ROADMAP_SKIP"] = "Sheaves,Cohomology"
    try:
        _run(w, opts, "auto")
        check("select: every open area skipped -> NoProgress", False)
    except tc.NoProgress as e:
        check("select: every open area skipped -> NoProgress", "roadmap-skip" in str(e) and str(tfile) in str(e))
    # A pin still beats a skip.
    cap.clear()
    _run(w, opts, "Sheaves")
    check("select: pinned area beats skip", "Start with `Sheaves`" in cap["prompt"])
    os.environ.pop("TAUCETI_ROADMAP_SKIP", None)

    # (c) a pinned area with no open items raises NoProgress, naming the file.
    for area in ("AllDone", "NotInTheFile"):
        try:
            _run(w, opts, area)
            check(f"select: pinned {area} without open items -> NoProgress", False)
        except tc.NoProgress as e:
            check(f"select: pinned {area} without open items -> NoProgress", area in str(e) and str(tfile) in str(e))

    # No open item anywhere -> NoProgress naming the file (auto).
    all_done = tmp / "done.md"
    all_done.write_text("<!-- tauceti-targets:v1 -->\n## Sheaves\n- [x] `a` — b (done: #1)\n")
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(all_done)
    try:
        _run(w, opts, "auto")
        check("select: no open targets anywhere -> NoProgress", False)
    except tc.NoProgress as e:
        check("select: no open targets anywhere -> NoProgress", "no open targets" in str(e) and str(all_done) in str(e))
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)

    # (d) the rendered block reaches the prompt on both paths, and the placeholder is filled.
    cap.clear()
    _run(w, opts, "Cohomology", bubble=True)
    prompt = cap["prompt"]
    check("prompt(bubble): __TARGETS__ filled", "__TARGETS__" not in prompt)
    check("prompt(bubble): preamble present", "Goal: discharge the nine GQ2 axioms." in prompt)
    check("prompt(bubble): area block present", "Targets in `Cohomology` (2 open of 3):" in prompt)
    check("prompt(bubble): resolved needs present", "needs: sheaf-pullback-exact [ ], ghost-slug [?]" in prompt)
    check("prompt(bubble): other areas not listed", "`stalk-functor` —" not in prompt)
    check("prompt(bubble): the operator-list rule is in the prompt", "**Operator target list.**" in prompt)
    check("prompt(bubble): block sits inside the rule's fence", "  ```\n  Goal: discharge" in prompt)
    check("prompt(bubble): assigned target precedes the fence", "Assigned target: `no-needs-clause`" in prompt)
    check(
        "prompt(bubble): context lead-in between assignment and fence",
        "  Context — the rest of this area's list:\n  ```\n  Goal: discharge" in prompt,
    )
    check("prompt(bubble): continuation lines indented under the bullet", "\n  - [ ] `derived-pushforward`" in prompt)
    check(
        "prompt(bubble): blank lines stay blank",
        "\n  \n" not in prompt.split("**Operator target list.**")[1].split("- **Follow")[0],
    )
    check("prompt(bubble): bubble target stays canonical", cap.get("target") == tc.constants.TAUCETI)
    cap.clear()
    _run(w, opts, "Cohomology", bubble=False)
    host = cap["host_prompt"]
    check("prompt(host): __TARGETS__ filled", "__TARGETS__" not in host)
    check("prompt(host): area block present", "Targets in `Cohomology` (2 open of 3):" in host)

    # A file that is unreadable or not a target list dies rather than authoring blind.
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tmp / "missing.md")
    try:
        _run(w, opts, "auto")
        check("select: missing file -> Die", False)
    except tc.Die as e:
        check("select: missing file -> Die", "missing.md" in str(e))
    bad = tmp / "bad.md"
    bad.write_text("# just notes\n## Sheaves\n- [ ] `a` — b\n")
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(bad)
    try:
        _run(w, opts, "auto")
        check("select: marker-less file -> Die", False)
    except tc.Die as e:
        check("select: marker-less file -> Die", "bad.md" in str(e) and "marker" in str(e))

    # (e) env unset: `none` in place of the block, and the ordinary area logic is untouched.
    for value in (None, "", "   "):
        os.environ.pop("TAUCETI_ROADMAP_TARGETS", None)
        if value is not None:
            os.environ["TAUCETI_ROADMAP_TARGETS"] = value
        check(f"config: roadmap_targets() is None for {value!r}", tc.config.roadmap_targets() is None)
    os.environ.pop("TAUCETI_ROADMAP_TARGETS", None)
    cap.clear()
    ST["acquires"].clear()
    _run(w, opts, "Topology")
    prompt = cap["prompt"]
    check("prompt(unset): `none` fills the block", "  ```\n  none\n  ```" in prompt)
    check("prompt(unset): assigned target is none", "  Assigned target: none\n  ```\n  none\n  ```" in prompt)
    check("prompt(unset): no claim taken by the worker", not ST["acquires"] and "TAUCETI_CLAIM_KEY" not in os.environ)
    check("prompt(unset): no target list text", "Targets in `" not in prompt)
    check("prompt(unset): pinned area still used", "Start with `Topology`" in prompt)
    # Unset + auto consults the area list as before (the stub proves it is called).
    tc.work_units.roadmap_areas = lambda gh: ["Alpha", "Beta"]
    cap.clear()
    _run(w, opts, "auto")
    check(
        "prompt(unset): auto still draws from roadmap_areas",
        any(f"Start with `{a}`" in cap["prompt"] for a in ("Alpha", "Beta")),
    )

    # The status-bar label names the file only when set.
    check("label: no targets suffix when unset", "targets:" not in tc.config._only_label())
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)
    check("label: targets basename appended", tc.config._only_label().endswith(" · targets: gq2-targets.md"))
    os.environ.pop("TAUCETI_ROADMAP_TARGETS", None)


# ---- 4. the live overlay -------------------------------------------------------------------------
def test_overlay():
    ids = S.target_marker_ids
    check("marker ids: focus+id pair", ids(_marker("Sheaves", "stalk-functor")) == (("Sheaves", "stalk-functor"),))
    check(
        "marker ids: several, sorted, deduplicated",
        ids(_marker("B", "y") + " text " + _marker("A", "x") + _marker("B", "y")) == (("A", "x"), ("B", "y")),
    )
    check("marker ids: no id -> dropped", ids('<!--tauceti-target:v1 {"focus":"Sheaves"}-->') == ())
    check("marker ids: malformed json -> dropped", ids('<!--tauceti-target:v1 {"focus":"S","id":}-->') == ())
    check("marker ids: focus any is kept (exact match later)", ids(_marker("any", "x")) == (("any", "x"),))
    p = S.PRInfo.from_json({"number": 1, "body": "a\n" + _marker("Cohomology", "no-needs-clause")})
    check("PRInfo.from_json: target_ids populated", p.target_ids == (("Cohomology", "no-needs-clause"),))
    check("PRInfo.from_json: target_focuses unchanged", p.target_focuses == ("Cohomology",))

    t = T.parse_targets(SAMPLE)
    warned = []
    saved_log = T.log
    T.log = lambda msg: warned.append(msg)
    try:
        # (a) an open marker puts an open item in flight; a merged marker marks an item done, even one
        # the file still shows in flight; an item the file already marks done stays done.
        live = T.overlay_live(
            t,
            inflight={("Cohomology", "no-needs-clause"), ("Sheaves", "site-basics")},
            done={("Sheaves", "stalk-functor")},
        )
        check("overlay: open marker -> in flight", live.find("no-needs-clause").status == "inflight")
        check("overlay: merged marker beats the file's [~]", live.find("stalk-functor").status == "done")
        check("overlay: file [x] stays done under an open marker", live.find("site-basics").status == "done")
        check("overlay: untouched item keeps its mark", live.find("sheaf-pullback-exact").status == "open")
        check("overlay: the source list is not mutated", t.find("stalk-functor").status == "inflight")
        check(
            "overlay: render shows effective statuses",
            "- [~] `no-needs-clause`" in T.render_area_block(live, "Cohomology"),
        )
        # (b) eligibility: open + every need done; needs resolve on the live view.
        check(
            "eligible: file view — pullback blocked on [~] stalk-functor",
            [it.slug for it in T.eligible_items(t, "Sheaves")] == ["sheaf-hom"],
        )
        check(
            "eligible: live view — pullback unblocked once stalk-functor merged",
            [it.slug for it in T.eligible_items(live, "Sheaves")] == ["sheaf-pullback-exact", "sheaf-hom"],
        )
        check(
            "eligible: in-flight item is not eligible", [it.slug for it in T.eligible_items(live, "Cohomology")] == []
        )
        check("eligible_areas: file order", T.eligible_areas(live) == ["Sheaves"])
        # (c) an unknown `needs` slug counts as done, with one warning naming it.
        warned.clear()
        live2 = T.overlay_live(t, inflight=set(), done={("Sheaves", "sheaf-pullback-exact")})
        check(
            "eligible: unknown need treated as done",
            "derived-pushforward" in [it.slug for it in T.eligible_items(live2, "Cohomology")],
        )
        check("eligible: unknown need warned once, by name", len(warned) == 1 and "ghost-slug" in warned[0])
        check(
            "eligible: pullback still blocked while sheaf-pullback-exact open",
            "derived-pushforward" not in [it.slug for it in T.eligible_items(t, "Cohomology")],
        )
    finally:
        T.log = saved_log

    # (d) _live_target_view: sv None is fine; merged comes from gh.pr_list(state="merged"); a
    # GitHubError falls back to the file's marks; markers for unlisted pairs are ignored.
    tmp = Path(tempfile.mkdtemp(prefix="targets-live-"))
    w, opts, cap = _stub_round(tmp)
    gh = FakeGitHub(merged_bodies=[_marker("Sheaves", "stalk-functor"), _marker("Sheaves", "not-listed"), "no marker"])
    live, n_in, n_done = tc.work_units._live_target_view(t, tmp / "f.md", None, gh)
    check("live view: sv None -> nothing in flight", n_in == 0)
    check("live view: merged fetch asks for merged PRs with bodies", gh.calls == [(("number", "body"), "merged")])
    check("live view: merged marker counted and applied", n_done == 1 and live.find("stalk-functor").status == "done")
    sv = _survey(("Cohomology", "no-needs-clause"), ("Nowhere", "no-needs-clause"))
    live, n_in, n_done = tc.work_units._live_target_view(t, tmp / "f.md", sv, None)
    check(
        "live view: open markers matched on (area, slug) only",
        n_in == 1 and live.find("no-needs-clause").status == "inflight",
    )
    check(
        "live view: gh None -> no merged fetch, file marks stand",
        n_done == 0 and live.find("stalk-functor").status == "inflight",
    )
    ST["log"].clear()
    live, n_in, n_done = tc.work_units._live_target_view(t, tmp / "f.md", sv, FakeGitHub(fail=True))
    check("live view: GitHubError -> file marks used", n_done == 0 and live.find("stalk-functor").status == "inflight")
    check(
        "live view: GitHubError logged, names the file",
        any("could not list merged PRs" in m and "f.md" in m for m in ST["log"]),
    )
    check("live view: GitHubError keeps the open overlay", n_in == 1)


# ---- 5. the worker-side claim loop -----------------------------------------------------------------
CLAIMS = """\
<!-- tauceti-targets:v1 -->
## Alpha
- [ ] `a-one` — first (serves: X)
- [ ] `a-two` — second (serves: X; needs: none)
- [ ] `a-three` — third (serves: X)

## Beta
- [ ] `b-one` — beta first (serves: Y)
"""


def test_claim_loop():
    tmp = Path(tempfile.mkdtemp(prefix="targets-claim-"))
    tfile = tmp / "claims.md"
    tfile.write_text(CLAIMS)
    w, opts, cap = _stub_round(tmp)
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)
    os.environ.pop("TAUCETI_ROADMAP_SKIP", None)

    # (a) rc [1, 0]: the first candidate is held elsewhere, the second is ours.
    ST["script"][:] = [1, 0]
    ST["acquires"].clear()
    ST["log"].clear()
    ST.pop("heartbeat", None)
    _run(w, opts, "Alpha")
    keys = [a[0] for a in ST["acquires"]]
    check(
        "claim: acquires in file order until one is ours",
        keys
        == [
            ("acquire", "author/Alpha/a-one", str(tc.constants.CLAIM_TTL_S)),
            ("acquire", "author/Alpha/a-two", str(tc.constants.CLAIM_TTL_S)),
        ],
    )
    check("claim: against the claim namespace", {a[1] for a in ST["acquires"]} == {"alice/tauceti-claims"})
    check("claim: push arbiter fails closed on this key", os.environ.get("TAUCETI_CLAIM_KEY") == "author/Alpha/a-two")
    check("claim: push arbiter told the namespace", os.environ.get("TAUCETI_CLAIM_REPO") == "alice/tauceti-claims")
    check(
        "claim: heartbeat started on the held key",
        ST.get("heartbeat") == ("author/Alpha/a-two", "alice/tauceti-claims"),
    )
    check("claim: lease recorded for release", w.claims.held == ("author/Alpha/a-two", "alice/tauceti-claims"))
    check("claim: release registered on cleanup", w.claims.release in ST["cleanups"])
    check(
        "claim: held candidate logged",
        any("target Alpha/a-one held by another worker — trying the next" in m for m in ST["log"]),
    )
    check(
        "claim: one summary line",
        any(
            m == "→ ROADMAP target: Alpha/a-two (claimed; 3 eligible of 4 open in 1 areas; live: 0 in flight, 0 merged)"
            for m in ST["log"]
        ),
    )
    prompt = cap["prompt"]
    check(
        "prompt: assigned target is the claimed slug",
        "  Assigned target: `a-two` — second (serves: X; needs: none)\n  Context — the rest of this area's list:\n  ```"
        in prompt,
    )
    check("prompt: the rule tells the agent it must author it", "You MUST author the assigned target" in prompt)
    check(
        "prompt: the claim step knows the worker holds the lease",
        "the worker already holds `author/<target-roadmap>/<slug>`" in prompt,
    )
    check(
        "prompt: area block still follows", "Targets in `Alpha` (3 open of 3):" in prompt and "- [ ] `a-one`" in prompt
    )
    check("prompt: pinned area used", "Start with `Alpha`" in prompt)

    # (b) every candidate held -> NoProgress, no lease, no env.
    ST["script"][:] = [1, 1, 1]
    ST["acquires"].clear()
    try:
        _run(w, opts, "Alpha")
        check("claim: all held -> NoProgress", False)
    except tc.NoProgress as e:
        check("claim: all held -> NoProgress", "claimed by another worker" in str(e) and str(tfile) in str(e))
    check("claim: all held -> three acquires, nothing kept", len(ST["acquires"]) == 3 and w.claims.held is None)
    check("claim: all held -> no push-arbiter key", "TAUCETI_CLAIM_KEY" not in os.environ)

    # (c) rc 2: the claim cannot be registered; the item is taken unclaimed with the hint logged.
    ST["script"][:] = [2]
    ST["acquires"].clear()
    ST["log"].clear()
    ST.pop("heartbeat", None)
    _run(w, opts, "Alpha")
    check(
        "claim: rc 2 -> first item taken unclaimed",
        len(ST["acquires"]) == 1 and "Assigned target: `a-one`" in cap["prompt"],
    )
    check(
        "claim: rc 2 -> no lease, no key, no heartbeat",
        w.claims.held is None and "TAUCETI_CLAIM_KEY" not in os.environ and "heartbeat" not in ST,
    )
    check("claim: rc 2 -> CLAIM_REPO hint logged once", sum("set CLAIM_REPO=" in m for m in ST["log"]) == 1)
    check(
        "claim: rc 2 -> summary says unclaimed",
        any(m.startswith("→ ROADMAP target: Alpha/a-one (unclaimed;") for m in ST["log"]),
    )

    # (d) the acquire cap: a long list of held items stops after MAX_TARGET_ACQUIRES pushes.
    many = tmp / "many.md"
    many.write_text(
        "<!-- tauceti-targets:v1 -->\n## Gamma\n" + "".join(f"- [ ] `g-{n}` — item {n}\n" for n in range(20))
    )
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(many)
    ST["script"][:] = [1] * 20
    ST["acquires"].clear()
    try:
        _run(w, opts, "Gamma")
        check("claim: cap -> NoProgress", False)
    except tc.NoProgress as e:
        check("claim: cap -> NoProgress names the cap", "attempt cap" in str(e) and "12 more untried" in str(e))
    check(
        "claim: cap -> exactly MAX_TARGET_ACQUIRES acquires",
        len(ST["acquires"]) == tc.work_units.MAX_TARGET_ACQUIRES == 8,
    )
    ST["script"].clear()
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(tfile)

    # (e) auto: areas are visited in random order, so the first acquire is not always Alpha.
    firsts = set()
    for _ in range(24):
        ST["acquires"].clear()
        _run(w, opts, "auto")
        firsts.add(_first_key())
    check("claim: auto spreads the first acquire across areas", firsts == {"author/Alpha/a-one", "author/Beta/b-one"})
    # Within an area the order is the file's.
    ST["script"][:] = [1, 1, 1, 0]
    ST["acquires"].clear()
    _run(w, opts, "auto")
    keys = [a[0][1] for a in ST["acquires"]]
    check(
        "claim: within an area, file order; then the other area",
        keys
        in (
            ["author/Alpha/a-one", "author/Alpha/a-two", "author/Alpha/a-three", "author/Beta/b-one"],
            ["author/Beta/b-one", "author/Alpha/a-one", "author/Alpha/a-two", "author/Alpha/a-three"],
        ),
    )

    # (f) the live overlay feeds selection: an open PR on a-one and a merged one on a-two leave a-three.
    ST["acquires"].clear()
    ST["log"].clear()
    w.gh = FakeGitHub(merged_bodies=[_marker("Alpha", "a-two")])
    _run(w, opts, "Alpha", sv=_survey(("Alpha", "a-one")))
    check(
        "claim: live view skips in-flight and merged items",
        [a[0][1] for a in ST["acquires"]] == ["author/Alpha/a-three"],
    )
    check(
        "claim: summary counts the live view",
        any("(claimed; 1 eligible of 2 open in 1 areas; live: 1 in flight, 1 merged)" in m for m in ST["log"]),
    )
    check(
        "prompt: context shows effective statuses",
        "- [~] `a-one`" in cap["prompt"]
        and "- [x] `a-two`" in cap["prompt"]
        and "Targets in `Alpha` (1 open of 3):" in cap["prompt"],
    )
    # All of an area's items covered live -> a pinned area is NoProgress; auto moves to the other area.
    w.gh = FakeGitHub(merged_bodies=[_marker("Alpha", "a-two"), _marker("Alpha", "a-three")])
    try:
        _run(w, opts, "Alpha", sv=_survey(("Alpha", "a-one")))
        check("claim: pinned area fully covered live -> NoProgress", False)
    except tc.NoProgress as e:
        check("claim: pinned area fully covered live -> NoProgress", "Alpha" in str(e))
    ST["acquires"].clear()
    _run(w, opts, "auto", sv=_survey(("Alpha", "a-one")))
    check("claim: auto skips a live-covered area", _first_key() == "author/Beta/b-one")
    # Blocked needs: an open item waiting on an open need is not a candidate, and the message says so.
    blocked = tmp / "blocked.md"
    blocked.write_text(
        "<!-- tauceti-targets:v1 -->\n## Delta\n- [ ] `d-two` — second (needs: `d-one`)\n- [~] `d-one` — first (in flight: #1)\n"
    )
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(blocked)
    w.gh = None
    try:
        _run(w, opts, "auto")
        check("claim: only blocked items -> NoProgress", False)
    except tc.NoProgress as e:
        check(
            "claim: only blocked items -> NoProgress explains",
            "wait on unmet needs" in str(e) and "1 open item" in str(e),
        )
    # (g) the assigned line: known needs are "all landed"; an unknown one is named, not called landed.
    ghost = tmp / "ghost.md"
    ghost.write_text(
        "<!-- tauceti-targets:v1 -->\n## Eps\n- [x] `e-one` — first (done: #1)\n"
        "- [ ] `e-two` — second (serves: Z; needs: `e-one`, `ghost`)\n"
    )
    os.environ["TAUCETI_ROADMAP_TARGETS"] = str(ghost)
    _run(w, opts, "Eps")
    check(
        "prompt: assigned line distinguishes landed from unknown needs",
        "Assigned target: `e-two` — second (serves: Z; needs: `e-one` — all landed; `ghost` — not in the list, "
        "assumed landed)" in cap["prompt"],
    )
    os.environ.pop("TAUCETI_ROADMAP_TARGETS", None)


def main():
    test_parser()
    test_helpers()
    test_selection()
    test_overlay()
    test_claim_loop()
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
