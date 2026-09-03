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

FORK = "alice/TauCeti"
fails = 0


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
        [it.slug for it in sh] == ["sheaf-pullback-exact", "stalk-functor", "site-basics"],
    )
    check("parser: statuses", [it.status for it in sh] == ["open", "inflight", "done"])
    check("parser: markers", [it.marker for it in sh] == ["[ ]", "[~]", "[x]"])
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
def _stub_round(tmp):
    """The same stubbing as tests/fork_authoring.py::test_roadmap, minus the source material."""
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
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=tmp, wid="worker3", checkout=tmp / "checkout", logdir=tmp / "logs"), gh=None
    )
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude", source=None)
    return w, opts, cap


def _run(w, opts, reason, bubble=True):
    return tc.work_units.do_roadmap(w, None, types.SimpleNamespace(reason=reason, pr=0, head=""), opts, bubble=bubble)


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
    _run(w, opts, "Topology")
    prompt = cap["prompt"]
    check("prompt(unset): `none` fills the block", "  ```\n  none\n  ```" in prompt)
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


def main():
    test_parser()
    test_helpers()
    test_selection()
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
