"""Pure bridge from GLM extraction proposals to Production Claim commands.

This module deliberately stops at the command boundary.  It does not create
Evidence Card rows, mapping proposals, manifests, or vector projections.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from knowledge.evidence import ClaimRole, MechanismStatus
from knowledge.governance import ReviewStatus
from knowledge.models import EntityType
from research.extraction.models import KnowledgeProposal
from research.review.workflow_models import (
    ClaimCandidateCommand,
    ExtractionProducer,
    ExtractionProvenanceInput,
)


class ProductionBridgeError(ValueError):
    """The proposal cannot safely cross into the Production review workflow."""


class UnresolvedEntityReference(BaseModel):
    """An extraction-time entity that has no approved canonical DB identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: EntityType
    extraction_entity_id: UUID
    name: str


class ProductionClaimSubmission(BaseModel):
    """A pending Claim command plus retry and resolution metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: ClaimCandidateCommand
    idempotency_key: str
    proposal_id: UUID
    extraction_evidence_card_id: UUID
    unresolved_mechanisms: tuple[UnresolvedEntityReference, ...] = ()
    unresolved_principles: tuple[UnresolvedEntityReference, ...] = ()


def build_production_claim_submissions(
    proposal: KnowledgeProposal,
    *,
    extraction_provenance: ExtractionProvenanceInput,
    resolved_entity_ids: Mapping[str, UUID] | None = None,
) -> tuple[ProductionClaimSubmission, ...]:
    """Convert one approved-source GLM proposal into pending Claim commands.

    ``resolved_entity_ids`` maps extraction-time entity proposal IDs to
    already-approved canonical Production entity IDs.  A reference is linked
    only when *all* references of that type resolve.  Otherwise the command is
    left unresolved and the missing references are returned alongside it.

    No persistence or network operation occurs here.
    """

    provenance = _validated_glm_provenance(extraction_provenance, proposal)
    source_version_id = _validated_source_binding(proposal)
    entity_index = _entity_index(proposal)
    resolutions = _normalize_resolutions(resolved_entity_ids)

    submissions: list[ProductionClaimSubmission] = []
    seen_card_ids: set[str] = set()
    for card in proposal.evidence_cards:
        if card.id in seen_card_ids:
            raise ProductionBridgeError("duplicate extraction Evidence Card ID")
        seen_card_ids.add(card.id)
        if card.claim_role == ClaimRole.CONTEXT_ONLY:
            raise ProductionBridgeError("context_only claims cannot enter Claim review")
        if card.created_by.casefold() != ExtractionProducer.GLM.value:
            raise ProductionBridgeError("Claim candidate is not GLM-produced")
        if card.application_origin.value == "reviewer_synthesis":
            raise ProductionBridgeError(
                "GLM-produced Claims cannot claim reviewer synthesis"
            )
        _validate_card_binding(proposal, card, source_version_id)

        mechanism_ids, unresolved_mechanisms = _resolve_references(
            card.mechanism_ids,
            expected_type=EntityType.COGNITIVE_MECHANISM,
            entity_index=entity_index,
            resolutions=resolutions,
        )
        principle_ids, unresolved_principles = _resolve_references(
            card.principle_ids,
            expected_type=EntityType.PSYCHOLOGY_PRINCIPLE,
            entity_index=entity_index,
            resolutions=resolutions,
        )
        command = ClaimCandidateCommand(
            source_version_id=source_version_id,
            claim=card.claim,
            claim_role=card.claim_role,
            claim_polarity=card.claim_polarity,
            proposed_evidence_class=card.evidence_class,
            applicable_domain=card.applicable_domain,
            ontology_paths=card.ontology_paths,
            topic_tags=card.topic_tags,
            mechanism_ids=mechanism_ids,
            mechanism_status=(
                MechanismStatus.LINKED
                if mechanism_ids
                else MechanismStatus.UNRESOLVED
            ),
            principle_ids=principle_ids,
            magic_application=card.magic_application,
            application_origin=card.application_origin,
            locator=card.locator,
            evidence_excerpt=card.evidence_excerpt,
            proposed_limitations=card.limitations,
            population_context=card.population_context,
            performance_context=card.performance_context,
            extraction_confidence=card.extraction_confidence,
            extraction_provenance=provenance,
        )
        submissions.append(
            ProductionClaimSubmission(
                command=command,
                idempotency_key=_claim_idempotency_key(command),
                proposal_id=UUID(proposal.id),
                extraction_evidence_card_id=UUID(card.id),
                unresolved_mechanisms=unresolved_mechanisms,
                unresolved_principles=unresolved_principles,
            )
        )
    return tuple(submissions)


def _validated_glm_provenance(
    provenance: ExtractionProvenanceInput,
    proposal: KnowledgeProposal,
) -> ExtractionProvenanceInput:
    try:
        validated = ExtractionProvenanceInput.model_validate(
            provenance.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProductionBridgeError("invalid GLM extraction provenance") from exc
    if validated.producer != ExtractionProducer.GLM:
        raise ProductionBridgeError("Production extraction bridge accepts GLM only")
    if validated.llm_provider != "GLM" or not validated.model:
        raise ProductionBridgeError("GLM provider and model are required")
    if validated.extraction_schema_version != proposal.schema_version:
        raise ProductionBridgeError(
            "GLM provenance schema version does not match proposal"
        )
    if not validated.run_id:
        raise ProductionBridgeError("GLM extraction run ID is required")
    try:
        proposal_run_id = UUID(proposal.extraction_run_id)
        provenance_run_id = UUID(validated.run_id)
    except (TypeError, ValueError) as exc:
        raise ProductionBridgeError("GLM extraction run ID must be a UUID") from exc
    if provenance_run_id != proposal_run_id:
        raise ProductionBridgeError("GLM provenance run ID does not match proposal")
    return validated.model_copy(update={"run_id": str(proposal_run_id)})


def _validated_source_binding(proposal: KnowledgeProposal) -> UUID:
    approval = proposal.source_approval
    if approval.status != ReviewStatus.APPROVED or not approval.allows_claim_extraction:
        raise ProductionBridgeError("Source version is not approved for claim extraction")
    try:
        source_version_id = UUID(approval.source_version_id)
        UUID(proposal.id)
    except (TypeError, ValueError) as exc:
        raise ProductionBridgeError("proposal/source version identity is invalid") from exc
    if approval.source_candidate_id != proposal.candidate.id:
        raise ProductionBridgeError("Source approval is not bound to proposal candidate")
    if approval.citation_id != proposal.citation.id:
        raise ProductionBridgeError("Source approval is not bound to proposal citation")
    for claim in proposal.context_claims:
        if claim.claim_role != ClaimRole.CONTEXT_ONLY:
            raise ProductionBridgeError(
                "non-context claim was placed in proposal context_claims"
            )
    return source_version_id


def _validate_card_binding(proposal, card, source_version_id: UUID) -> None:
    source = card.source
    try:
        card_source_version_id = UUID(source.source_version_id)
    except (TypeError, ValueError) as exc:
        raise ProductionBridgeError("Evidence Card source version is invalid") from exc
    if card_source_version_id != source_version_id:
        raise ProductionBridgeError("Evidence Card source version does not match approval")
    if source.source_candidate_id != proposal.candidate.id:
        raise ProductionBridgeError("Evidence Card is not bound to proposal candidate")
    if source.document_id != proposal.candidate.id:
        raise ProductionBridgeError("Evidence Card document does not match proposal candidate")
    if source.citation_id != proposal.citation.id:
        raise ProductionBridgeError("Evidence Card citation does not match proposal citation")


def _entity_index(proposal: KnowledgeProposal) -> dict[str, tuple[EntityType, str]]:
    output: dict[str, tuple[EntityType, str]] = {}
    for mapping in proposal.entity_proposals:
        entity_id = str(UUID(mapping.entity.id))
        existing = output.get(entity_id)
        descriptor = (mapping.entity.type, mapping.entity.name)
        if existing is not None and existing != descriptor:
            raise ProductionBridgeError("conflicting extraction entity identity")
        output[entity_id] = descriptor
    return output


def _normalize_resolutions(
    values: Mapping[str, UUID] | None,
) -> dict[str, UUID]:
    output: dict[str, UUID] = {}
    for extraction_id, canonical_id in (values or {}).items():
        try:
            output[str(UUID(str(extraction_id)))] = UUID(str(canonical_id))
        except (TypeError, ValueError) as exc:
            raise ProductionBridgeError("resolved entity IDs must be UUIDs") from exc
    return output


def _resolve_references(
    reference_ids: list[str],
    *,
    expected_type: EntityType,
    entity_index: Mapping[str, tuple[EntityType, str]],
    resolutions: Mapping[str, UUID],
) -> tuple[list[UUID], tuple[UnresolvedEntityReference, ...]]:
    if not reference_ids:
        return [], ()
    resolved: list[UUID] = []
    unresolved: list[UnresolvedEntityReference] = []
    for raw_id in dict.fromkeys(reference_ids):
        try:
            extraction_id = str(UUID(raw_id))
        except (TypeError, ValueError) as exc:
            raise ProductionBridgeError("extraction entity reference is not a UUID") from exc
        entity = entity_index.get(extraction_id)
        if entity is None or entity[0] != expected_type:
            unresolved.append(
                UnresolvedEntityReference(
                    entity_type=expected_type,
                    extraction_entity_id=UUID(extraction_id),
                    name=entity[1] if entity is not None else extraction_id,
                )
            )
            continue
        canonical_id = resolutions.get(extraction_id)
        if canonical_id is None:
            unresolved.append(
                UnresolvedEntityReference(
                    entity_type=expected_type,
                    extraction_entity_id=UUID(extraction_id),
                    name=entity[1],
                )
            )
        else:
            resolved.append(canonical_id)
    if unresolved:
        # Partial linking falsely implies that the extraction mapping is fully
        # resolved.  Fail closed while retaining the unresolved review detail.
        return [], tuple(unresolved)
    return list(dict.fromkeys(resolved)), ()


def _claim_idempotency_key(command: ClaimCandidateCommand) -> str:
    payload = json.dumps(
        command.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"glm-claim-{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ProductionBridgeError",
    "ProductionClaimSubmission",
    "UnresolvedEntityReference",
    "build_production_claim_submissions",
]
