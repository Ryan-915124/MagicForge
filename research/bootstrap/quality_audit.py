"""Reproducible GLM-only semantic audit for isolated bootstrap artifacts.

This module never approves sources or claims.  Its output is a machine audit
used to decide whether another bootstrap extraction pass is warranted.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from app.config import get_settings
from knowledge.bootstrap import BootstrapStorageManifest
from knowledge.evidence import EvidenceCard
from llm.glm_client import GLMClient


DEFAULT_RUN = Path("research/runs/bootstrap-001-v02")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--card-sample-size", type=int, default=30)
    parser.add_argument("--node-sample-size", type=int, default=20)
    parser.add_argument("--seed", type=int, default=2002)
    args = parser.parse_args()
    report = audit(
        args.run,
        card_sample_size=args.card_sample_size,
        node_sample_size=args.node_sample_size,
        seed=args.seed,
    )
    destination = args.run / "reports" / "automated-semantic-audit.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


def audit(
    run: Path,
    *,
    card_sample_size: int,
    node_sample_size: int,
    seed: int,
) -> dict[str, object]:
    settings = get_settings()
    llm = GLMClient(settings.glm_api_key, settings.glm_model)
    manifest = BootstrapStorageManifest.model_validate_json(
        (run / "qdrant_manifest" / "bootstrap-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    retained_card_ids = {
        projection.evidence_card_id
        for projection in manifest.projections
        if projection.evidence_card_id
    }
    adapter = TypeAdapter(list[EvidenceCard])
    cards = [
        card
        for path in sorted((run / "evidence_cards").glob("*.json"))
        for card in adapter.validate_json(path.read_text(encoding="utf-8"))
        if card.id in retained_card_ids
    ]
    registry = json.loads(
        (run / "knowledge_nodes" / "_canonical_registry.json").read_text(
            encoding="utf-8"
        )
    )
    rng = random.Random(seed)
    sampled_cards = rng.sample(cards, min(card_sample_size, len(cards)))
    nodes = registry["nodes"]
    sampled_nodes = rng.sample(nodes, min(node_sample_size, len(nodes)))
    relationships = registry["relationships"]
    card_lookup = {card.id: card for card in cards}

    card_items = [
            {
                "id": card.id,
                "claim": card.claim,
                "excerpt": card.evidence_excerpt,
                "claim_role": card.claim_role.value,
                "evidence_class": card.evidence_class.value,
                "source_type": card.source.source_type.value,
                "knowledge_origin": card.knowledge_origin.value,
                "limitations": card.limitations,
            }
            for card in sampled_cards
        ]
    card_decisions = _judge_complete_batches(llm, card_items, _judge_cards)

    node_items = [
            {
                "id": node["id"],
                "entity_type": node["entity"]["type"],
                "name": node["entity"]["name"],
                "definition": node["definition"],
                "ontology_paths": node["ontology_paths"],
            }
            for node in sampled_nodes
        ]
    node_decisions = _judge_complete_batches(llm, node_items, _judge_nodes)
    relationship_decisions = _judge_relationships(
        llm,
        [
            {
                "id": item["id"],
                "source": item["source_entity_name"],
                "relation": item["relationship"]["type"],
                "target": item["target_entity_name"],
                "assertion": item["assertion"],
                "support": [
                    {
                        "claim": card_lookup[card_id].claim,
                        "excerpt": card_lookup[card_id].evidence_excerpt,
                    }
                    for card_id in item["supporting_evidence_ids"]
                    if card_id in card_lookup
                ],
            }
            for item in relationships
        ],
    )

    metrics = {
        "audit_kind": "automated_glm_self_audit_not_human_review",
        "card_sample_size": len(card_decisions),
        "claim_support_pass_rate": _rate(card_decisions, "claim_supported"),
        "claim_role_pass_rate": _rate(card_decisions, "claim_role_correct"),
        "evidence_class_pass_rate": _rate(card_decisions, "evidence_class_correct"),
        "node_sample_size": len(node_decisions),
        "entity_type_pass_rate": _rate(node_decisions, "entity_type_correct"),
        "ontology_placement_pass_rate": _rate(
            node_decisions, "ontology_placement_correct"
        ),
        "relationship_sample_size": len(relationship_decisions),
        "relationship_validity_pass_rate": _rate(
            relationship_decisions, "relationship_entailed"
        ),
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run.name,
        "seed": seed,
        "human_review_performed": False,
        "governance_state_changed": False,
        "limitations": [
            "The same GLM family generated and audited the proposals, so correlated error remains possible.",
            "A positive machine audit is not source approval, claim approval, or human verification.",
            "Contradicting evidence was not checked in this bootstrap audit.",
        ],
        "metrics": metrics,
        "evidence_card_decisions": card_decisions,
        "knowledge_node_decisions": node_decisions,
        "relationship_decisions": relationship_decisions,
    }


def _judge_cards(llm: GLMClient, items: list[dict[str, object]]) -> list[dict]:
    prompt = f"""Audit unverified MagicForge Evidence Card candidates.
For each item decide:
- claim_supported: the excerpt directly supports the claim without stronger causality, scope, or certainty.
- claim_role_correct: result=reported finding; method=procedure; background=prior/general context; hypothesis=untested proposition; discussion=interpretation/implication; expert_opinion=practitioner judgment.
- evidence_class_correct: classify this individual claim, not the document type. Background/hypothesis are not controlled experiments. Practitioner material cannot be scientific evidence merely because it discusses psychology.
Be strict. Return every input id exactly once. Reasons must be under 30 words.
Return JSON only: {{"items":[{{"id":"...","claim_supported":true,"claim_role_correct":true,"evidence_class_correct":true,"reason":"..."}}]}}

ITEMS:
{json.dumps(items, ensure_ascii=False)}"""
    return _items(_generate_json(llm, prompt), {str(item["id"]) for item in items})


def _judge_nodes(llm: GLMClient, items: list[dict[str, object]]) -> list[dict]:
    prompt = f"""Audit unverified MagicForge Knowledge Node candidates.
Entity rules: Effect is an audience-perceived event; Technique an executable skill; Method a secret implementation; Performer a named person/group; CognitiveMechanism a scientific explanatory construct; PsychologyPrinciple an applied psychological concept. Reject generic nouns, brain regions/measures, experiment groups/conditions, and routine titles used as techniques.
ontology_placement_correct requires dot-only paths whose meaning matches both type and definition.
Return every input id exactly once. Reasons under 30 words.
Return JSON only: {{"items":[{{"id":"...","entity_type_correct":true,"ontology_placement_correct":true,"reason":"..."}}]}}

ITEMS:
{json.dumps(items, ensure_ascii=False)}"""
    return _items(_generate_json(llm, prompt), {str(item["id"]) for item in items})


def _judge_relationships(llm: GLMClient, items: list[dict[str, object]]) -> list[dict]:
    if not items:
        return []
    prompt = f"""Audit unverified MagicForge relationship candidates.
performed_by must be Effect -> Performer. requires needs explicit necessity. explains needs an explanatory claim. uses needs explicit actual usage. inspired_by needs explicit origin/inspiration. related_to is fallback only. Co-mention, correlation, activation, and experimental stimuli do not entail stronger relations.
relationship_entailed is true only if the supplied claim/excerpt explicitly supports the directed triple.
Return every input id exactly once. Reasons under 30 words.
Return JSON only: {{"items":[{{"id":"...","relationship_entailed":true,"reason":"..."}}]}}

ITEMS:
{json.dumps(items, ensure_ascii=False)}"""
    return _items(_generate_json(llm, prompt), {str(item["id"]) for item in items})


def _judge_complete_batches(
    llm: GLMClient,
    items: list[dict[str, object]],
    judge,
    *,
    batch_size: int = 8,
) -> list[dict]:
    """Require complete ID coverage, splitting incomplete model responses.

    Semantic audits are advisory, but their accounting must still be exact. A
    large structured response can occasionally omit an item.  We fail closed
    for a single-item mismatch and recursively reduce larger batches instead
    of accepting partial coverage.
    """

    decisions: list[dict] = []
    for start in range(0, len(items), batch_size):
        decisions.extend(
            _judge_complete_batch(
                llm,
                items[start : start + batch_size],
                judge,
            )
        )
    expected_ids = {str(item["id"]) for item in items}
    actual_ids = {str(item.get("id")) for item in decisions}
    if actual_ids != expected_ids or len(decisions) != len(items):
        raise ValueError("audit batch aggregation did not preserve every input ID")
    return decisions


def _judge_complete_batch(
    llm: GLMClient,
    items: list[dict[str, object]],
    judge,
) -> list[dict]:
    if not items:
        return []
    try:
        return judge(llm, items)
    except (ValueError, json.JSONDecodeError):
        if len(items) == 1:
            raise
        midpoint = len(items) // 2
        return [
            *_judge_complete_batch(llm, items[:midpoint], judge),
            *_judge_complete_batch(llm, items[midpoint:], judge),
        ]


def _generate_json(llm: GLMClient, prompt: str) -> dict:
    raw = llm.generate(
        prompt,
        system_prompt=(
            "You are a strict semantic quality auditor. Use only supplied fields, "
            "do not approve anything, and return valid JSON."
        ),
        temperature=0.0,
        max_tokens=4_000,
        json_mode=True,
        thinking_enabled=False,
    ).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    return json.loads(raw)


def _items(response: dict, expected_ids: set[str]) -> list[dict]:
    values = response.get("items")
    if not isinstance(values, list):
        raise ValueError("audit response lacks items")
    actual_ids = {str(item.get("id")) for item in values if isinstance(item, dict)}
    if actual_ids != expected_ids or len(values) != len(expected_ids):
        raise ValueError("audit response IDs do not match the requested sample")
    return values


def _rate(items: list[dict], field: str) -> float:
    if not items:
        return 0.0
    return round(sum(item.get(field) is True for item in items) / len(items) * 100, 1)


if __name__ == "__main__":
    main()
