import pytest

from app.reveal_parser import parse_magicforge_reveal


def test_parses_complete_reveal_with_lead_and_synthesis() -> None:
    raw = """A close reading reveals three layers.

[[MAGICFORGE_ACT:EFFECT]]
The audience remembers an impossible disappearance.
[[MAGICFORGE_ACT:HIDDEN_STRUCTURE]]
Timing separates the secret action from the visible effect.
[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]
Attention and memory reconstruction support the experience.
[[MAGICFORGE_SYNTHESIS]]
Design the memory of the effect, not only the move."""

    parsed = parse_magicforge_reveal(raw)

    assert parsed is not None
    assert parsed.lead == "A close reading reveals three layers."
    assert [(act.kind, act.content) for act in parsed.acts] == [
        ("effect", "The audience remembers an impossible disappearance."),
        (
            "hidden_structure",
            "Timing separates the secret action from the visible effect.",
        ),
        (
            "cognitive_mechanism",
            "Attention and memory reconstruction support the experience.",
        ),
    ]
    assert parsed.synthesis == "Design the memory of the effect, not only the move."


def test_allows_skipped_acts_and_an_unclosed_final_segment() -> None:
    raw = """[[MAGICFORGE_ACT:EFFECT]]
The spectator sees a prediction become true.
[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]
Expectation shapes interpretation even when the response ends here"""

    parsed = parse_magicforge_reveal(raw)

    assert parsed is not None
    assert parsed.lead is None
    assert [act.kind for act in parsed.acts] == [
        "effect",
        "cognitive_mechanism",
    ]
    assert parsed.synthesis is None


def test_ignores_empty_optional_acts_when_another_act_has_content() -> None:
    raw = """[[MAGICFORGE_ACT:EFFECT]]
[[MAGICFORGE_ACT:HIDDEN_STRUCTURE]]
The method is displaced in time."""

    parsed = parse_magicforge_reveal(raw)

    assert parsed is not None
    assert [(act.kind, act.content) for act in parsed.acts] == [
        ("hidden_structure", "The method is displaced in time.")
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "A normal legacy answer without markers.",
        "Inline [[MAGICFORGE_ACT:EFFECT]] text is not a marker.",
        "[[MAGICFORGE_ACT:EFFECT]]   \nTrailing spaces prevent recognition.",
        "[[MAGICFORGE_ACT:EFFECT]]\n   \n[[MAGICFORGE_SYNTHESIS]]\nSummary only.",
        "[[MAGICFORGE_SYNTHESIS]]\nSummary without a non-empty act.",
    ],
)
def test_falls_back_when_there_is_no_nonempty_recognized_act(raw: str) -> None:
    assert parse_magicforge_reveal(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        """[[MAGICFORGE_ACT:EFFECT]]
Effect.
[[MAGICFORGE_ACT:EFFECT]]
Repeated.""",
        """[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]
Science.
[[MAGICFORGE_ACT:HIDDEN_STRUCTURE]]
Out of order.""",
        """[[MAGICFORGE_ACT:EFFECT]]
Effect.
[[MAGICFORGE_ACT:METHOD]]
Unknown marker.""",
        """[[MAGICFORGE_ACT:EFFECT]]
Effect.
[[MAGICFORGE_ARCHIVE]]
Unknown marker.""",
    ],
)
def test_malformed_marker_sequences_use_complete_legacy_fallback(raw: str) -> None:
    assert parse_magicforge_reveal(raw) is None


def test_exact_markers_inside_code_fences_are_content_not_structure() -> None:
    raw = """```text
[[MAGICFORGE_ACT:EFFECT]]
```
[[MAGICFORGE_ACT:HIDDEN_STRUCTURE]]
The real structured layer."""

    parsed = parse_magicforge_reveal(raw)

    assert parsed is not None
    assert parsed.lead == "```text\n[[MAGICFORGE_ACT:EFFECT]]\n```"
    assert [(act.kind, act.content) for act in parsed.acts] == [
        ("hidden_structure", "The real structured layer.")
    ]


def test_unclosed_code_fence_in_final_act_is_treated_as_truncated_content() -> None:
    raw = """[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]
The explanation includes notation:
```text
[[MAGICFORGE_SYNTHESIS]]
still inside the unfinished code fence"""

    parsed = parse_magicforge_reveal(raw)

    assert parsed is not None
    assert len(parsed.acts) == 1
    assert "[[MAGICFORGE_SYNTHESIS]]" in parsed.acts[0].content
    assert parsed.synthesis is None


def test_supports_crlf_input_without_changing_the_raw_contract() -> None:
    parsed = parse_magicforge_reveal(
        "Lead\r\n[[MAGICFORGE_ACT:EFFECT]]\r\nEffect\r\n"
        "[[MAGICFORGE_SYNTHESIS]]\r\nSummary\r\n"
    )

    assert parsed is not None
    assert parsed.lead == "Lead"
    assert parsed.acts[0].content == "Effect"
    assert parsed.synthesis == "Summary"
