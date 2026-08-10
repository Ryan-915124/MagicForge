# MagicForge Extraction Pipeline v0.1

## Purpose and non-negotiable boundary

This pipeline transforms a human-approved source into reviewable, provenance-backed magic knowledge. It does not treat document storage as knowledge acquisition.

The required flow is:

```text
Source
  -> human source approval
  -> claim extraction
  -> Evidence Cards
  -> human claim and mapping approval
  -> Knowledge Nodes and relations
  -> explicit storage authorization
  -> embedding
  -> Qdrant
```

The following shortcut is prohibited, even when the PDF is lawfully accessible:

```text
PDF -> chunks -> embedding -> Qdrant       # INVALID
```

PDF, Markdown, and text chunks may exist temporarily as extraction context. They are not trusted knowledge objects and must never be sent directly to Qdrant. Qdrant receives only human-approved projections of Evidence Cards, Knowledge Nodes, and their provenance.

GLM remains the only LLM. It proposes structure; a named human decides whether any proposal is valid, eligible, and storable. No new LLM provider and no graph database are introduced.

## Pipeline overview

```text
unverified candidate
       |
       v
[0] Source registration and citation verification
       |
       v
[1] Human source/extraction approval -------------------- fail/return/reject
       |
       v
[2] Permitted document extraction (PDF/MD/TXT)
       |
       v
[3] Locator-preserving temporary chunking
       |
       v
[4] GLM claim/entity/relation proposals
       |
       v
[5] Evidence Card assembly and validation
       |
       v
[6] Human claim and mapping approval -------------------- fail/return/reject
       |
       v
[7] Canonical Knowledge Nodes + relations
       |
       v
[8] Storage manifest and explicit authorization --------- fail/hold
       |
       v
[9] Approved retrieval projection + embedding
       |
       v
[10] Idempotent Qdrant upsert + ingestion receipt
```

Stages 0–8 operate outside Qdrant. Discovery snippets and unreviewed extraction output never cross the storage boundary.

## Stage contracts

### Stage 0 — Source registration and citation verification

**Input**

- `ResearchCandidate` from the completed discovery round
- search provenance, provider result identity, canonical URL/DOI
- citation metadata and access state

**Process**

- Normalize source identity and deduplicate by DOI, canonical URL, then normalized title.
- Verify title, author, year, venue, edition/version, and DOI where applicable.
- Record whether access is metadata, abstract, web extract, full text, or PDF text.
- Compute a stable source ID and, when content is acquired, a content checksum.

**Output**

- Versioned Source Record
- Citation Record with verification state and audit evidence
- Source-review item in `pending`

**Failure gate**

- An unverified citation, ambiguous edition, missing provenance, or search snippet alone cannot proceed to content claim extraction.
- Failure records remain outside the extraction queue; no embedding occurs.

### Stage 1 — Human source and extraction approval

**Input**

- Versioned Source Record and citation
- proposed extraction scope
- access and rights information
- evidence-class proposal and known concerns

**Process**

A named source reviewer records reviewer, review date, reason, claim eligibility, extraction permission, storage permission, sensitivity level, and contradiction-check state as specified by the Human Review Workflow.

**Output**

- Source Approval Record bound to an exact source version
- allowed extraction mode: `metadata_only`, `selected_sections`, or `full_text`

**Failure gate**

- `pending`, held, returned, rejected, `claim_eligibility=ineligible`, or `extraction_permission=none` stops the pipeline.
- Source approval authorizes extraction only. It does not approve a claim or authorize storage.

### Stage 2 — Permitted document extraction

**Input**

- Source Approval Record
- approved local PDF, Markdown, text file, or approved MCP-fetched source content

**Process**

- Select a format adapter through `ExtractionRegistry`.
- PDF extraction preserves page numbers and page locators.
- Markdown extraction preserves headings and declared metadata without treating declarations as verified facts.
- Text extraction preserves a stable source locator.
- Limit processing to the human-approved section/page scope.
- Store the extraction artifact in a controlled working area, not Qdrant.

**Output**

- `ExtractedDocument` containing ordered sections and locators
- Extraction Manifest: adapter/version, source checksum, permissions, extraction time, and warnings

**Failure gate**

- Image-only PDFs fail with “OCR required”; v0.1 does not silently produce empty content.
- Corrupt files, missing pages, extraction outside the permitted scope, or changed content checksum stop processing and return the source for review.
- Search snippets are discovery evidence only and are rejected by the extractor.

### Stage 3 — Locator-preserving temporary chunking

**Input**

- `ExtractedDocument`
- versioned chunking policy (`chunk_size`, overlap, section/page boundary rules)

**Process**

- Split only to fit extraction context and preserve page, heading, order, and source locator.
- Mark chunks as `temporary_extraction_context`.
- Keep adjacent context references so claim reviewers can inspect the surrounding source.

**Output**

- Ordered `RawChunk` work items with deterministic IDs
- Chunk Manifest tied to source checksum and chunking-policy version

**Failure gate**

- A chunk without traceable source identity and locator is ineligible for claim extraction.
- Temporary chunks have no route to the embedding or Qdrant interfaces.

### Stage 4 — GLM structured proposal generation

**Input**

- One untrusted temporary chunk
- source title and locator
- versioned extraction schema and prompt

**Process**

- Delimit external text and treat instructions inside it as untrusted data.
- Ask GLM at deterministic settings to propose atomic claims, entities, relations, mechanisms, limitations, conflicts, and short supporting excerpts.
- Permit only the canonical entity/relation vocabulary defined by the ontology.
- Create `Source` and `ResearchPaper` identity from verified provenance, never from GLM output.
- Do not infer a method from an effect description or a scientific mechanism from a practitioner assertion.

**Output**

- Schema-valid `ResearchExtractionResult` proposals
- model/prompt/schema fingerprints and extraction-run ID

**Failure gate**

- Invalid JSON, unsupported entity/relation types, absent excerpts, missing locators, out-of-range confidence, prompt-injection indicators, or insufficient evidence produce no eligible claim.
- Empty proposal lists are valid when evidence is insufficient.
- GLM confidence is advisory and cannot open a review or storage gate by itself.

### Stage 5 — Evidence Card assembly

**Input**

- Validated GLM proposals
- verified citation and source version
- extraction locator and permission scope

**Process**

- Normalize each atomic proposal into an Evidence Card.
- Attach claim, mechanism, evidence class, source ID, excerpt and locator, provisional confidence, limitations, applicable domain, magic application, and conflicting-card references.
- Label origin explicitly as scientific empirical, scientific review/theory, expert practitioner, anecdotal, or MagicForge interpretation.
- Deduplicate exact claim proposals while retaining all independent provenance links.
- Validate that quotation length and storage scope conform to the Source Approval Record.

**Output**

- Evidence Card candidates in `pending`
- unresolved entity/mapping proposals
- conflict candidates, never silently merged

**Failure gate**

- Missing evidence, locator, evidence class, source version, or applicable domain makes a card ineligible.
- Practitioner books may produce practitioner-knowledge cards, but they cannot be automatically classified as scientific evidence.
- MagicForge interpretations must be separately labeled and cannot masquerade as source claims.

### Stage 6 — Human claim and mapping approval

**Input**

- Evidence Card candidates
- source context at the recorded locator
- proposed entities, relations, limitations, conflicts, and exposure level

**Process**

- A named human verifies each atomic claim against the source.
- The reviewer finalizes evidence class, confidence, limitations, applicability, and contradiction status.
- A domain reviewer confirms entity identity, relation direction, terminology, and secret-exposure handling.
- Approvals, returns/revisions, and rejections are recorded per card and mapping.

**Output**

- Immutable approved Evidence Card versions
- approved entity/relation mapping proposals
- audit events containing reviewer, date, reason, and checked conflicts

**Failure gate**

- Unreviewed, returned, rejected, contradicted-without-linkage, or permission-incompatible cards do not proceed.
- Approval of one card does not approve sibling cards from the same source.

### Stage 7 — Canonical Knowledge Nodes and relations

**Input**

- Approved Evidence Cards and mapping decisions
- MagicForge ontology and entity-resolution rules

**Process**

- Resolve or create storage-neutral nodes such as `Effect`, `Method`, `Technique`, `PsychologyPrinciple`, `CognitiveMechanism`, `Performer`, `Source`, and `ResearchPaper`.
- Build only approved relations (`uses`, `inspired_by`, `requires`, `explains`, `performed_by`, `related_to`).
- Link every asserted property and relation to one or more Evidence Card IDs.
- Preserve scientific evidence, expert practice knowledge, and MagicForge interpretation as separate assertions even when they concern the same node.
- Use stable IDs and graph-compatible records without installing a graph database.

**Output**

- Versioned Knowledge Nodes and Knowledge Relationships
- provenance edges to Evidence Cards and Sources
- change set showing creates, merges, supersessions, and unresolved collisions

**Failure gate**

- Dangling relation endpoints, unresolved identity collisions, unsupported relation direction, missing evidence linkage, or sensitivity-policy mismatch return the mapping for review.

### Stage 8 — Storage manifest and explicit authorization

**Input**

- Approved Evidence Card versions
- approved node/relation versions
- source storage permissions and sensitivity classifications
- target Qdrant payload schema version

**Process**

- Build an immutable manifest listing every card, node, relation, and retrieval projection.
- Validate that all included objects are approved and that storage permission covers their derived representation and excerpts.
- Exclude raw documents and every `temporary_extraction_context` chunk.
- Hash the complete manifest and request a separate named-human storage authorization.

**Output**

- Authorized Storage Manifest
- expected point IDs/count and authorization audit event

**Failure gate**

- Any unapproved object, missing permission, expired scope, unhandled sensitive information, raw source text, or manifest mutation blocks ingestion.

### Stage 9 — Retrieval projection and embedding

**Input**

- Authorized Storage Manifest only

**Process**

- Render a concise retrieval text from approved fields, for example: claim, mechanism, definition, limitations, magic application, applicable domain, and approved practitioner/scientific label.
- Attach filterable metadata, provenance IDs, approval status, sensitivity level, evidence level, confidence, schema versions, and manifest hash.
- Generate embeddings through the existing embedding boundary. This stage introduces no additional LLM provider.

**Output**

- Qdrant-ready points whose text is a reviewed knowledge projection, not source prose
- embedding model/version fingerprint and payload checksum

**Failure gate**

- Projection schema failure, missing review metadata, unauthorized excerpts, or embedding failure produces no Qdrant authorization receipt and does not change review status.

### Stage 10 — Idempotent Qdrant upsert and receipt

**Input**

- Authorized, embedded points and expected point manifest

**Process**

- Upsert deterministic point IDs in a non-retrievable staging state (`review_status=approved`).
- Verify accepted count, the complete point ID set, payload checksums, and staging status.
- Promote the exact manifest point set to `review_status=ingested`, then verify it again.
- Record the ingestion actor and UTC timestamp.

**Output**

- Ingestion Receipt linking source/card/node versions to exact Qdrant point IDs
- review item transitions from `approved` to `ingested` only after complete verification

**Failure gate**

- If Qdrant accepts fewer points than expected, staging points remain hidden and the review item remains `approved`; no Ingestion Receipt is issued.
- A retry uses the same deterministic IDs and manifest. A changed artifact requires a new manifest and authorization.

## What is embedded

A point should represent a useful, independently reviewable unit rather than a page-sized document fragment. Permitted projection types include:

1. **Evidence projection** — approved claim, mechanism, evidence class, limitations, applicability, source/card IDs, and an allowed short excerpt.
2. **Knowledge-node projection** — approved definition or domain description plus links to supporting/contradicting Evidence Cards.
3. **Relation projection** — approved relation statement with endpoints, scope, provenance, and confidence.
4. **Practice projection** — explicitly labeled expert/practitioner guidance with attribution and limitations.
5. **Interpretation projection** — explicitly labeled MagicForge synthesis with all input Evidence Card IDs.

Full source text, full PDF pages, temporary chunks, unreviewed summaries, and discovery snippets are not permitted Qdrant projection types.

## Identity, idempotency, and versioning

### Stable identities

- `source_id`: deterministic from DOI or canonical URL/title fallback.
- `source_version_id`: `source_id` plus edition/version and content checksum.
- `raw_chunk_id`: `source_version_id` plus chunk-policy version, locator, index, and normalized text hash. This ID is working-state only.
- `extraction_run_id`: source version + chunk policy + extraction schema + prompt + GLM model/version fingerprint.
- `evidence_card_id`: source version + locator + normalized atomic claim + evidence-schema version.
- `knowledge_node_id`: canonical entity type + normalized identity, consistent with the storage-neutral entity model.
- `relation_id`: source node + relation type + target node + assertion scope.
- `projection_point_id`: approved card/node/relation ID + projection-schema version.
- `storage_manifest_id`: hash of sorted projection IDs, versions, permissions, and payload checksums.

### Idempotency rules

| Operation | Idempotency key | Repeat behavior |
|---|---|---|
| Register candidate | DOI/canonical URL/title identity | Return the existing source record and append discovery provenance |
| Extract content | Source version + adapter/version + scope | Reuse a valid extraction artifact; never silently replace changed content |
| Chunk content | Source version + chunk-policy version | Produce the same ordered work IDs |
| Run GLM extraction | Extraction-run ID | Reuse cached validated proposal or create a separately versioned rerun |
| Submit review | Deterministic proposal/card version ID | Return the existing pending/reviewed item; do not duplicate decisions |
| Build knowledge objects | Approved card IDs + ontology version | Produce a deterministic change set |
| Authorize storage | Exact manifest hash | Authorization applies only to that immutable manifest |
| Qdrant upsert | Deterministic projection point IDs | Safely retry and verify; changed content creates a new version/manifest |

Version fields must be explicit: ontology version, evidence schema version, extraction schema/prompt version, chunk-policy version, projection schema version, embedding model/version, and review-policy version. Re-extraction does not erase prior approvals; it creates a new version that must pass review again.

## Failure recovery and supersession

- Failures before source approval create no extraction artifacts beyond diagnostic records.
- File extraction and GLM failures may be retried under the same run identity only when inputs and versions are unchanged.
- Invalid GLM output is quarantined as an extraction error, not repaired into an approved claim automatically.
- Returned cards remain pending and retain all revision events.
- Rejected versions are immutable. A corrected card is a new linked version.
- Partial Qdrant writes do not produce an `ingested` state. Deterministic IDs allow verification and retry against the same manifest.
- Corrected or contradicted knowledge is superseded by a newly reviewed version. Previous records retain provenance and audit history; retrieval excludes superseded projections through metadata policy rather than destructive history rewriting.

## Permissions, sensitivity, and exposure

Storage rights and retrieval exposure are separate controls:

- `Extraction permission` controls what source material the pipeline may process.
- `Storage permission` controls whether derived knowledge and short excerpts may be persisted.
- `Sensitive information level` controls who may retrieve the approved projection.
- `secret_exposure_level` in the Qdrant metadata implements filtering, but cannot grant permission that the source review denied.

Copyrighted practitioner works can contribute only within recorded access and extraction scope. Possessing a book does not convert its statements into scientific evidence, and permission to extract it does not authorize storing its raw prose.

## Current implementation alignment

The current repository already provides useful foundations:

- `ExtractionRegistry` supports PDF, Markdown, and text with page/heading locators.
- `ResearchKnowledgeExtractor` rejects search snippets, treats source text as untrusted, and uses GLM structured output.
- deterministic source version, Evidence Card, proposal, assertion, projection, manifest, and receipt IDs support idempotency.
- `SourceApprovalService` binds extraction permission to an exact content hash and scope.
- `HumanReviewService`/`ClaimReviewService` reviews one Evidence Card at a time; `MappingReviewService` separately reviews entity and relationship assertions.
- `ApprovedKnowledgeIngestor` accepts only a named-human-authorized Storage Manifest and records an Ingestion Receipt.
- `QdrantService` rejects legacy chunks and applies fail-closed review, permission, sensitivity, and exposure filters to every query.

The former `KnowledgeChunk` write contract and `knowledge.ingestion` CLI are disabled. `RawChunk` and legacy `KnowledgeChunk` objects remain usable only as temporary parsing/migration artifacts and are rejected at the vector write boundary. `ResearchKnowledgeExtractor` now returns pending Evidence Cards and mapping proposals rather than Qdrant-ready chunks.

These implementation changes do not approve any source or claim and do not run extraction, embeddings, or a real Qdrant write by themselves.

## Acceptance criteria for a future implementation

- No source without a scoped, named-human approval can be extracted for claims.
- No search snippet can become evidence.
- No PDF/Markdown/text chunk can call the embedding or Qdrant interface directly.
- Every stored claim has an approved Evidence Card, exact locator, source version, evidence class, limitations, and contradiction status.
- Practitioner knowledge and MagicForge interpretation remain distinguishable from scientific evidence in both payload and retrieval.
- Every Knowledge Node assertion and relation resolves to approved Evidence Card IDs.
- Storage uses an immutable, explicitly authorized manifest and deterministic point IDs.
- A partial write never marks a review item `ingested`.
- The system can reproduce who approved each point, when, why, and from which source/card/node versions.

## Related specifications

- [MagicForge Human Review Workflow v0.1](review-workflow-v0.1.md)
- [MagicForge Evidence Schema v0.1](evidence-schema-v0.1.md)
- [MagicForge Ontology v0.1](magicforge-ontology-v0.1.md)
- [MagicForge Qdrant Schema v0.1](qdrant-schema-v0.1.md)
