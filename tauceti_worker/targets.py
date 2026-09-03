"""tauceti_worker.targets — the operator's target list for `--roadmap-targets`.

An operator who wants workers to author only PRs on the path to one specific goal (say, discharging
the nine axioms of an external formalization) writes a markdown list of milestones, grouped by
roadmap area, and points the worker at it. That is finer-grained than `--roadmap-only`: the worker
restricts its area pick to areas that still have open items, and the prompt names exactly which
milestones are in scope. The file is re-read every round, so the operator ticks items off (or marks
them in flight) while the loop runs.

The format (`parse_targets` is lenient about whitespace, strict about nothing else):

    # GQ2 axiom targets
    <!-- tauceti-targets:v1 -->
    free-text preamble, until the first `## ` heading

    ## <Area>                                  the exact roadmap directory name
    - [ ] `slug` — text (serves: B10; needs: `a-slug`, `b-slug`)
    - [~] `slug` — text (serves: B3c; needs: none; in flight: #5504)
    - [x] `slug` — text (serves: B1; done: #5513)
    > note lines, and anything else, are ignored

`[ ]` is open, `[~]` in flight, `[x]` done. The slug is the first backtick token on the line; the
text is what follows the first ` — ` up to the trailing parenthesised metadata; `needs:` (optional)
is a comma list of backtick slugs or `none`. A `## Gaps…` section is skipped, and a `- [` line that
does not parse is warned about once, never fatal. The marker comment is mandatory: without it the
file is not a target list and the round dies rather than authoring against a misread file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Die, log

MARKER_RE = re.compile(r"<!--\s*tauceti-targets:v1\s*-->")
_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
_ITEM_RE = re.compile(r"^\s*-\s*\[\s*([ xX~])\s*\]\s*(.*?)\s*$")
_ITEM_START_RE = re.compile(r"^\s*-\s*\[")
_SLUG_RE = re.compile(r"`([^`]+)`")
_META_RE = re.compile(r"\(([^()]*)\)\s*$")
_STATUS = {" ": "open", "~": "inflight", "x": "done", "X": "done"}
MARKERS = {"open": "[ ]", "inflight": "[~]", "done": "[x]"}


@dataclass
class TargetItem:
    slug: str
    status: str  # "open" | "inflight" | "done"
    text: str  # the description between ` — ` and the trailing metadata
    needs: list[str]  # prerequisite slugs (empty for `none` or an absent `needs:`)
    area: str
    line: str = ""  # the whole source line, as written
    meta: list[str] = field(default_factory=list)  # the other `key: value` clauses, verbatim

    @property
    def marker(self) -> str:
        return MARKERS[self.status]


@dataclass
class Targets:
    preamble: str
    areas: dict[str, list[TargetItem]]  # area -> items in file order; insertion order = file order

    def find(self, slug: str) -> TargetItem | None:
        for items in self.areas.values():
            for it in items:
                if it.slug == slug:
                    return it
        return None


def _parse_item(raw: str, area: str) -> TargetItem | None:
    m = _ITEM_RE.match(raw)
    if not m:
        return None
    status, rest = _STATUS[m.group(1)], m.group(2)
    slug_m = _SLUG_RE.search(rest)
    if not slug_m:
        return None
    slug = slug_m.group(1).strip()
    body = rest[slug_m.end() :]
    # The description is what follows the first ` — ` (an em dash; a spaced hyphen is accepted too).
    sep = re.search(r"\s+[—–-]\s+", body)
    text = body[sep.end() :] if sep else body.strip()
    needs: list[str] = []
    meta: list[str] = []
    meta_m = _META_RE.search(text)
    if meta_m:
        text = text[: meta_m.start()].rstrip()
        for clause in (c.strip() for c in meta_m.group(1).split(";")):
            if not clause:
                continue
            key, _, value = clause.partition(":")
            if key.strip().lower() == "needs":
                value = value.strip()
                if value.lower() != "none":
                    needs = [s.strip() for s in _SLUG_RE.findall(value)]
                    if not needs:  # bare slugs without backticks — be lenient
                        needs = [s.strip() for s in value.split(",") if s.strip()]
            else:
                meta.append(clause)
    return TargetItem(slug=slug, status=status, text=text.strip(), needs=needs, area=area, line=raw.rstrip(), meta=meta)


def parse_targets(text: str) -> Targets:
    """Parse a target list; raise Die when the `tauceti-targets:v1` marker is missing."""
    if not MARKER_RE.search(text):
        raise Die("roadmap targets: the file lacks the `<!-- tauceti-targets:v1 -->` marker — not a target list")
    preamble_lines: list[str] = []
    areas: dict[str, list[TargetItem]] = {}
    area: str | None = None  # None before the first heading and inside a Gaps section
    seen_heading = False
    bad: list[int] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        h = _HEADING_RE.match(line)
        if h:
            seen_heading = True
            title = h.group(1)
            if title.lower().startswith("gaps"):
                area = None
            else:
                area = title
                areas.setdefault(area, [])
            continue
        if not seen_heading:
            if MARKER_RE.search(line) or line.startswith("# "):
                continue
            preamble_lines.append(line)
            continue
        if area is None:
            continue
        if _ITEM_START_RE.match(line):
            item = _parse_item(line, area)
            if item is None:
                bad.append(n)
                continue
            areas[area].append(item)
    if bad:
        shown = ", ".join(str(n) for n in bad[:5]) + (", …" if len(bad) > 5 else "")
        log(f"roadmap targets: ignoring {len(bad)} malformed item line(s) (line {shown})")
    preamble = "\n".join(preamble_lines).strip()
    preamble = re.sub(r"\n{3,}", "\n\n", preamble)
    return Targets(preamble=preamble, areas=areas)


def load_targets(path: Path) -> Targets:
    """Read and parse the operator's target list; any read or parse failure is a Die."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise Die(f"roadmap targets: cannot read {path}: {e}") from None
    try:
        return parse_targets(text)
    except Die as e:
        raise Die(f"roadmap targets: {path}: {e}") from None


def open_items(targets: Targets, area: str) -> list[TargetItem]:
    return [it for it in targets.areas.get(area, []) if it.status == "open"]


def open_areas(targets: Targets) -> list[str]:
    """Areas with at least one `[ ]` item, in file order."""
    return [a for a, items in targets.areas.items() if any(it.status == "open" for it in items)]


def render_item(targets: Targets, it: TargetItem) -> str:
    """One item line for the prompt: its marker, slug and text, the metadata it carried, and every
    prerequisite with its CURRENT status resolved across all areas (`[?]` for a slug the file does
    not define anywhere)."""
    clauses = list(it.meta)
    if it.needs:
        resolved = []
        for slug in it.needs:
            dep = targets.find(slug)
            resolved.append(f"{slug} {dep.marker if dep is not None else '[?]'}")
        clauses.append("needs: " + ", ".join(resolved))
    else:
        clauses.append("needs: none")
    return f"- {it.marker} `{it.slug}` — {it.text} ({'; '.join(clauses)})"


def render_area_block(targets: Targets, area: str) -> str:
    """The text substituted for `__TARGETS__`: the preamble, then the area's items in file order."""
    parts = []
    if targets.preamble:
        parts.append(targets.preamble)
    items = targets.areas.get(area, [])
    header = f"Targets in `{area}` ({sum(it.status == 'open' for it in items)} open of {len(items)}):"
    body = "\n".join(render_item(targets, it) for it in items) if items else "(no targets listed for this area)"
    parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)
