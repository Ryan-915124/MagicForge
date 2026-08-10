"""Deterministic semantic gates for GLM-proposed domain entities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.models import EntityType
from research.extraction.models import ExtractedEntity


@dataclass(frozen=True, slots=True)
class EntityValidationDecision:
    accepted: bool
    reason: str


_GENERIC_NAMES = {
    "audience",
    "audiences",
    "effect",
    "experienced magician",
    "illusion",
    "magic",
    "magic effect",
    "magic method",
    "magic technique",
    "magic trick",
    "magic tricks",
    "magician",
    "magicians",
    "method",
    "observer",
    "observers",
    "participant",
    "participants",
    "performance",
    "performer",
    "performers",
    "professional magician",
    "spectator",
    "spectators",
    "technique",
    "forcing technique",
    "psychological mechanisms",
    "information processing mechanism",
    "anticipation and preparation",
    "unified audience feeling",
}

_BRAIN_REGION_TERMS = (
    "amygdala",
    "brain region",
    "caudate",
    "cortex",
    "cortical",
    "frontal gyrus",
    "frontal lobe",
    "hippocampus",
    "insula",
    "nucleus",
    "parietal lobe",
    "putamen",
    "striatum",
)

_EXPERIMENT_CONDITION_TERMS = (
    "control condition",
    "control group",
    "deceived group",
    "deceptive performance",
    "experimental condition",
    "normal sample",
    "occluded condition",
    "stimulus condition",
    "undeceived group",
    "veridical performance",
)

_AUDIENCE_EFFECT_TERMS = (
    "appear",
    "appearance",
    "audience experiences",
    "change",
    "disappear",
    "impossible knowledge",
    "illusion",
    "levitation",
    "floating",
    "perceived",
    "prediction",
    "restoration",
    "seemingly",
    "seems to",
    "spectator experiences",
    "transformation",
    "transposition",
    "vanish",
)

_TECHNIQUE_TERMS = (
    "attention control",
    "equivoque",
    "false transfer",
    "force",
    "forcing",
    "gaze",
    "gesture",
    "load",
    "misdirection",
    "movement",
    "patter",
    "palm",
    "retention",
    "riffle",
    "script",
    "shuffle",
    "skill",
    "sleight",
    "steal",
    "switch",
    "timing",
    "transfer",
    "verbal cue",
)

_METHOD_TERMS = (
    "force",
    "gimmick",
    "hidden",
    "implementation",
    "information control",
    "mechanical",
    "method",
    "secret",
)

_PSYCHOLOGY_PRINCIPLES = (
    "agency",
    "anchoring",
    "assumption",
    "attentional focus",
    "awareness",
    "change blindness",
    "covert attention",
    "encoding limitation",
    "false memory",
    "gaze following",
    "inattentional blindness",
    "intent attribution",
    "illusory choice",
    "joint attention",
    "memory distortion",
    "overt attention",
    "reconstruction",
    "selective attention",
    "priming",
    "social cue",
    "social influence",
    "suggestion",
    "surprise",
    "trust",
    "visual attention",
    "violation of expectation",
)

_COGNITIVE_MECHANISMS = (
    "agency",
    "attentional capacity",
    "attentional capture",
    "attentional load",
    "attentional orienting",
    "attentional selection",
    "bayesian",
    "choice blindness",
    "causal reasoning",
    "cognitive",
    "expectation violation",
    "inhibition",
    "joint attention",
    "memory reconstruction",
    "mental model",
    "motor expertise",
    "perceptual",
    "perceptual filling",
    "prediction error",
    "predictive coding",
    "processing",
    "sensory persistence",
    "visuomotor",
)


def canonical_entity_key(entity_type: EntityType, name: str) -> str:
    normalized = re.sub(r"[^\w]+", " ", name.casefold(), flags=re.UNICODE).strip(" _")
    if entity_type == EntityType.TECHNIQUE:
        normalized = re.sub(r"\bforcing\b", "force", normalized)
        normalized = re.sub(r"\s+technique$", "", normalized)
    return f"{entity_type.value}:{' '.join(normalized.split())}"


def validate_extracted_entity(item: ExtractedEntity) -> EntityValidationDecision:
    name = " ".join(item.name.casefold().split())
    description = " ".join((item.description or "").casefold().split())
    excerpt = " ".join(item.evidence_excerpt.casefold().split())
    combined = " ".join((name, description, excerpt))
    identity_text = " ".join((name, description))

    if name in _GENERIC_NAMES or len(re.findall(r"[a-z0-9]+", name)) == 0:
        return EntityValidationDecision(False, "generic noun is not a canonical entity")
    if any(term in name for term in _EXPERIMENT_CONDITION_TERMS):
        return EntityValidationDecision(False, "experiment condition is not a domain entity")
    if item.type == EntityType.COGNITIVE_MECHANISM and any(
        term in name for term in _BRAIN_REGION_TERMS
    ):
        return EntityValidationDecision(False, "brain region is not a cognitive mechanism")

    if item.type == EntityType.EFFECT:
        if any(
            term in identity_text
            for term in (
                "intervention",
                "divergent thinking",
                "training outcome",
                "routine",
            )
        ):
            return EntityValidationDecision(False, "training or intervention outcome is not an audience effect")
        if any(
            term in identity_text
            for term in (
                "change blindness",
                "change detection",
                "cueing effect",
                "detection rate",
                "prediction error",
                "response time",
                "sleight",
                "french drop",
            )
        ):
            return EntityValidationDecision(
                False,
                "experimental outcome, psychology construct, or secret skill is not an audience effect",
            )
        if not any(term in identity_text for term in _AUDIENCE_EFFECT_TERMS):
            return EntityValidationDecision(False, "effect must be an audience-perceived event")
    elif item.type == EntityType.TECHNIQUE:
        if any(
            term in identity_text
            for term in (
                "routine",
                "performance piece",
                "stage act",
                "complete trick",
                "crib sheet",
                "recording and transcription",
                "techniques of",
            )
        ):
            return EntityValidationDecision(False, "routine title cannot be a technique")
        if name in {"social cue", "social cues"}:
            return EntityValidationDecision(False, "social cue is a stimulus, not an executable skill")
        if not any(term in identity_text for term in _TECHNIQUE_TERMS):
            return EntityValidationDecision(False, "technique must be an executable performance skill")
    elif item.type == EntityType.METHOD:
        if any(term in identity_text for term in ("program", "curriculum", "intervention", "workshop")):
            return EntityValidationDecision(False, "training program is not a secret implementation")
        if not any(term in identity_text for term in _METHOD_TERMS):
            return EntityValidationDecision(False, "method must describe secret implementation")
    elif item.type == EntityType.PERFORMER:
        if any(
            term in identity_text
            for term in (
                "researcher",
                "participant",
                "observer",
                "reviewer",
                "goats",
                "sheep",
            )
        ):
            return EntityValidationDecision(False, "research roles are not magic performers")
        if re.fullmatch(r"(?:experienced |professional |hungarian )?magician", name):
            return EntityValidationDecision(False, "unnamed magician is not a canonical performer")
        if name in _GENERIC_NAMES or not any(
            token[:1].isupper() for token in item.name.split() if token
        ):
            return EntityValidationDecision(False, "performer must be a named person or group")
    elif item.type == EntityType.COGNITIVE_MECHANISM:
        if "misdirection" in name:
            return EntityValidationDecision(False, "misdirection is a technique/model, not a cognitive mechanism")
        if name in _PSYCHOLOGY_PRINCIPLES:
            return EntityValidationDecision(False, "applied psychology principle is not a cognitive mechanism")
        if any(
            term in identity_text
            for term in (
                "pupil dilatation",
                "pupil dilation",
                "visuomotor system",
                "failure to notice",
                "physiological indicator",
            )
        ):
            return EntityValidationDecision(False, "measure, outcome, or neural system is not a cognitive mechanism")
        if not any(term in combined for term in _COGNITIVE_MECHANISMS):
            return EntityValidationDecision(False, "mechanism must be a scientific explanatory construct")
    elif item.type == EntityType.PSYCHOLOGY_PRINCIPLE:
        if any(term in identity_text for term in _BRAIN_REGION_TERMS):
            return EntityValidationDecision(False, "brain region is not a psychology principle")
        # The canonical name itself must identify an applied psychological
        # construct.  A vague label must not pass merely because its prose
        # description happens to mention surprise, trust, or attention.
        if not any(term in name for term in _PSYCHOLOGY_PRINCIPLES):
            return EntityValidationDecision(False, "psychology principle must be an applied psychological concept")

    return EntityValidationDecision(True, "entity satisfies deterministic type constraints")


__all__ = [
    "EntityValidationDecision",
    "canonical_entity_key",
    "validate_extracted_entity",
]
