"""Deterministic parser for Magic Chat's optional layered-reveal format."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


RevealActKind = Literal[
    "effect",
    "hidden_structure",
    "cognitive_mechanism",
]


@dataclass(frozen=True)
class ParsedRevealAct:
    kind: RevealActKind
    content: str


@dataclass(frozen=True)
class ParsedMagicForgeReveal:
    lead: str | None
    acts: tuple[ParsedRevealAct, ...]
    synthesis: str | None


_MARKER_TO_KIND: dict[str, RevealActKind] = {
    "[[MAGICFORGE_ACT:EFFECT]]": "effect",
    "[[MAGICFORGE_ACT:HIDDEN_STRUCTURE]]": "hidden_structure",
    "[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]": "cognitive_mechanism",
}
_SYNTHESIS_MARKER = "[[MAGICFORGE_SYNTHESIS]]"
_KNOWN_MARKERS = (*_MARKER_TO_KIND, _SYNTHESIS_MARKER)
_MARKER_ORDER = {marker: index for index, marker in enumerate(_KNOWN_MARKERS)}
_MAGICFORGE_MARKER_LINE = re.compile(r"\[\[MAGICFORGE_[^\[\]\r\n]+\]\]")
_OPENING_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def parse_magicforge_reveal(raw_result: str) -> ParsedMagicForgeReveal | None:
    """Parse a well-formed layered answer, or return ``None`` for legacy text.

    Markers are deliberately strict: they must occupy an entire line, occur
    outside Markdown code fences, follow the declared order, and never repeat.
    Any malformed MagicForge marker sequence causes a complete legacy fallback,
    leaving callers free to render ``raw_result`` unchanged.
    """

    lead_lines: list[str] = []
    section_lines: dict[str, list[str]] = {}
    active_marker: str | None = None
    last_marker_order = -1
    saw_marker = False
    fence: tuple[str, int] | None = None

    for line in raw_result.splitlines():
        if fence is not None:
            _append_line(lead_lines, section_lines, active_marker, line)
            if _is_closing_fence(line, fence):
                fence = None
            continue

        opening_fence = _OPENING_FENCE.fullmatch(line)
        if opening_fence is not None:
            fence_run = opening_fence.group(2)
            fence = (fence_run[0], len(fence_run))
            _append_line(lead_lines, section_lines, active_marker, line)
            continue

        if line in _MARKER_ORDER:
            marker_order = _MARKER_ORDER[line]
            if marker_order <= last_marker_order:
                return None
            saw_marker = True
            last_marker_order = marker_order
            active_marker = line
            section_lines[line] = []
            continue

        if _MAGICFORGE_MARKER_LINE.fullmatch(line):
            return None

        _append_line(lead_lines, section_lines, active_marker, line)

    if not saw_marker:
        return None

    acts = tuple(
        ParsedRevealAct(kind=kind, content=content)
        for marker, kind in _MARKER_TO_KIND.items()
        if (content := _clean(section_lines.get(marker, [])))
    )
    if not acts:
        return None

    return ParsedMagicForgeReveal(
        lead=_clean(lead_lines),
        acts=acts,
        synthesis=_clean(section_lines.get(_SYNTHESIS_MARKER, [])),
    )


def _append_line(
    lead_lines: list[str],
    section_lines: dict[str, list[str]],
    active_marker: str | None,
    line: str,
) -> None:
    if active_marker is None:
        lead_lines.append(line)
    else:
        section_lines[active_marker].append(line)


def _clean(lines: list[str]) -> str | None:
    content = "\n".join(lines).strip()
    return content or None


def _is_closing_fence(line: str, fence: tuple[str, int]) -> bool:
    character, minimum_length = fence
    match = re.fullmatch(r" {0,3}([`~]+)[ \t]*", line)
    if match is None:
        return False
    run = match.group(1)
    return (
        set(run) == {character}
        and len(run) >= minimum_length
    )
