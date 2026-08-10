# MagicForge Research Pipeline v0.1

## Boundary

MagicForge keeps three decisions independent:

```text
Source Approval != Claim Approval != Storage Authorization
```

GLM is the only LLM and may only propose structure. Qdrant is a rebuildable retrieval projection, not the evidence source of truth. No graph database is installed.

## Runtime flow

```text
ResearchCandidate + CitationRecord
  -> SourceApprovalService (named human, exact content hash and scope)
  -> ExtractionRegistry + temporary RawChunk
  -> ResearchKnowledgeExtractor (GLM structured candidates)
  -> pending Evidence Cards + Mapping Proposals
  -> ClaimReviewService + MappingReviewService (named humans)
  -> approved KnowledgeNodeVersion / RelationshipAssertion
  -> ProjectionBuilder
  -> immutable StorageManifest
  -> StorageManifestService.authorize (named human)
  -> ApprovedKnowledgeIngestor
  -> QdrantService.write_manifest
  -> IngestionReceipt
```

Raw PDF pages, Markdown/Text chunks, search snippets, unreviewed GLM output, and legacy `KnowledgeChunk` objects have no production storage route.

## Modules

```text
knowledge/
  evidence.py             Evidence Card and confidence schema
  governance.py           shared approval, permission, sensitivity enums
  projections.py          Knowledge assertions, Qdrant Projection, manifest/receipt
  manifest_repository.py  non-Qdrant manifest and receipt persistence ports

research/
  search/                  Exa/Tavily discovery and content acquisition
  citation/                citation verification and audit evidence
  extraction/              approved-scope GLM proposal extraction
  review/                  source, claim, mapping, and storage gates
  pipeline.py              orchestration that deliberately has no ingestion method

retrieval/
  interfaces.py            read-side retriever and write-side ProjectionWriter
  qdrant_service.py         hard-filtered retrieval and manifest-only writes
```

## Gate rules

### G1 — Source Approval

- Binds a decision to `source_candidate_id`, `citation_id`, exact SHA-256 content hash, source version, access mode, and approved locators.
- Requires verified citation state, acquired content, a named reviewer, reason, claim eligibility, and selected/full-text extraction permission.
- Does not approve any claim and does not authorize Qdrant.

### G2 — Claim and mapping review

- Each Evidence Card is reviewed independently.
- Extraction confidence remains separate from reviewer-assessed evidence confidence.
- Evidence class fixes its epistemic channel: scientific evidence, expert practice, or personal interpretation.
- Contradiction status and check record are mandatory for approval.
- Entity and relationship mappings require their exact approved supporting card IDs.

### G3 — Storage authorization

- `ProjectionBuilder` renders structured text and never uses `evidence_excerpt` as embedding text.
- `StorageManifest` content-addresses the exact point IDs and payload checksums.
- A named human authorizes the immutable manifest.
- `QdrantService` first writes hidden staging points, verifies the complete manifest,
  promotes only that exact point set to `ingested`, and verifies it again.
- `ApprovedKnowledgeIngestor` stores an `IngestionReceipt` and changes claim/manifest state only after both Qdrant checks succeed.

## Retrieval security

Every query receives non-optional server-side Qdrant conditions:

- `approved=true`
- `claim_eligibility=true`
- `storage_permission=true`
- `review_status=ingested`
- allowed `sensitive_information_level`
- `secret_exposure_rank <= requester clearance`
- `confidence_label != insufficient`

Callers may add domain, ontology, evidence-channel, entity, and relation filters, but cannot remove these conditions. When authorization context is absent, the service defaults to public/controlled material at general-principle exposure or lower.

## Persistence and recovery

Evidence Cards, knowledge assertions, review records, manifests, and receipts live outside Qdrant through replaceable repository protocols. Point IDs, manifest IDs, and artifact version IDs are deterministic. A retry of the same authorized manifest is idempotent; an altered artifact requires a new manifest and human authorization.

The default collection is `magicforge_knowledge_v01`. Existing document-chunk collections are legacy and must not be relabelled as approved knowledge.
