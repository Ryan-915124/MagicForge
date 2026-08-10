# MagicForge Human Review Workflow v0.1

## Status and scope

This document defines the review boundary for MagicForge Knowledge Architecture v0.1. It extends the information recorded by the current review queue; it does **not** approve any existing candidate, weaken a gate, or change the runtime transitions implemented by `HumanReviewService` and `ApprovedKnowledgeIngestor`.

The central invariant is:

```text
source approval != claim approval != ingestion authorization
```

- **Source approval** says that an identified source may be acquired and examined within a recorded scope.
- **Claim approval** says that one atomic claim is supported strongly enough, with the stated evidence class and limitations, to become a reviewed Evidence Card.
- **Ingestion authorization** says that a specific, versioned set of approved Evidence Cards and Knowledge Nodes may be projected into Qdrant under the recorded storage and sensitivity policy.

None of these decisions may be supplied by GLM, an automated pipeline, or the discovery provider. A named human makes every approval decision.

## Three independent review gates

| Gate | Object under review | Human decision | Result | What the result does **not** mean |
|---|---|---|---|---|
| G1 — Source and extraction | `ResearchCandidate`, citation, access record, proposed extraction scope | Approve for extraction, keep pending/return, or reject | A versioned Source Approval Record | It does not validate claims and does not permit Qdrant storage |
| G2 — Claim and knowledge | Atomic claim, Evidence Card, source excerpt and locator, proposed entity/relation mapping | Approve, return for revision, or reject each claim/mapping | Approved Evidence Cards and Knowledge Node proposals | It does not turn practitioner advice into scientific evidence and does not trigger ingestion |
| G3 — Storage | An immutable bundle of approved cards/nodes plus permissions and projection manifest | Explicitly authorize ingestion or decline | Storage Authorization and, after a successful write, an Ingestion Receipt | It does not permit raw documents or raw extraction chunks to enter Qdrant |

Each gate has its own actor, timestamp, reason, object version, and audit event. One person may perform more than one role when project staffing requires it, but the decisions remain separate records.

## Review roles

| Role | Responsibility | May not do |
|---|---|---|
| Research operator | Registers candidates, verifies provenance, obtains permitted content, and prepares review material | Approve their own work implicitly or treat discovery snippets as evidence |
| Source reviewer | Checks identity, citation, access rights, scope, relevance, and extraction permission | Approve individual claims merely by approving the source |
| Claim reviewer | Compares every claim and excerpt with the source, checks evidence class, limitations, conflicts, and applicability | Promote unsupported inference or practitioner opinion to empirical evidence |
| Domain reviewer | Checks magic terminology, entity resolution, relation direction, method attribution, and exposure sensitivity | Infer a secret method from an effect description |
| Storage authorizer | Confirms claim approvals, storage permission, sensitivity controls, and the exact projection manifest | Authorize raw PDFs, raw chapters, or unreviewed chunks for Qdrant |
| Ingestion operator | Executes the already-authorized, deterministic Qdrant write and records its outcome | Change review decisions during ingestion |
| GLM | Proposes structured claims, entities, relations, classifications, and possible conflicts | Act as reviewer, decide confidence finally, grant permission, or ingest autonomously |

## Review record design

### Existing source queue fields

The existing manual queue fields remain intact:

- `ID`
- `Candidate`
- `Evidence class`
- `Access state`
- `Main concern`
- `Recommended reviewer action`
- `Decision`

They retain their present meaning. In particular, `Evidence class` describes the kind of source or candidate material; it is not itself a quality verdict. A practitioner book remains practitioner knowledge unless a separately reviewed scientific source supports the same claim.

### Required added fields

| Field | Type / allowed values | Review meaning and gate effect |
|---|---|---|
| `Reviewer` | Non-empty human identity | Required for any approval or rejection; names such as `system`, `automation`, `llm`, and `glm` are invalid |
| `Review date` | ISO 8601 timestamp with timezone | Time of the human decision; distinct from submission and ingestion times |
| `Approval reason` | Non-empty text for approval; rejection reason is recorded in the same audit-note channel | Explains why the scoped action is allowed and cites remaining restrictions; cannot be replaced by a generic “looks good” |
| `Claim eligibility` | `not_assessed`, `eligible`, `eligible_with_limits`, `ineligible` | A source-level screening judgment about whether claim extraction is worthwhile. `eligible` only permits claims to be proposed; every claim still requires G2 review |
| `Extraction permission` | `none`, `metadata_only`, `selected_sections`, `full_text` plus an optional scope expression | Records what may be processed. Permission must reflect access rights and may be narrower than available content |
| `Storage permission` | `none`, `derived_knowledge_only`, `derived_with_short_excerpt` | Controls the reviewed output allowed in storage. It never authorizes raw PDFs, full chapters, or raw document chunks in Qdrant |
| `Sensitive information level` | `public`, `controlled`, `secret_method`, `restricted` | Sets exposure-aware retrieval and reviewer requirements. `secret_method` and `restricted` require explicit domain/sensitivity review before G3 |
| `Contradicting evidence checked` | `not_checked`, `checked_none_found`, `checked_conflicts_linked`, `not_applicable` | Approval of an evidence claim requires a completed value. When conflicts exist, their Evidence Card IDs must be linked rather than silently averaged away |

For machine-readable records, use stable snake_case names (`reviewer`, `review_date`, and so on) while preserving the human-readable headings above in exported Markdown.

`Sensitive information level` is the governance classification for the reviewed object. It is distinct from Qdrant's `secret_exposure_level`, which describes how much operational secret detail a particular retrieval projection contains. A projection must satisfy both controls; neither value may silently grant permission denied by the other.

### Supporting provenance and audit fields

The following fields make the required additions auditable:

- `object_id` and `object_version`
- `source_version_id` and content checksum
- `decision_scope` — source, claim, mapping, or storage bundle
- `decision` and `decision_reason`
- `previous_event_id`, when the event revises an earlier pending item
- `evidence_card_ids` and `knowledge_node_ids`, when applicable
- `permission_scope` and expiry/review date, when a license or policy is time-bound
- `conflicting_evidence_card_ids`
- `created_at`, `submitted_at`, `reviewed_at`, and `ingested_at`
- immutable `audit_log` entries containing action, actor, timestamp, and notes

Blank required values fail closed: the item remains pending and no downstream gate opens.

## Evidence-class separation

Every claim-level review must preserve one of these origins:

| Knowledge origin | Permitted interpretation |
|---|---|
| Scientific empirical evidence | A claim supported by a reviewed controlled, observational, or qualitative study, with design-specific limitations |
| Scientific review/theory | A synthesis or theoretical claim; it must not be mislabeled as a direct experimental result |
| Expert practitioner knowledge | A named practitioner's documented method, principle, or recommendation; valuable but not scientific evidence by default |
| Anecdotal practitioner report | A reported experience or case, clearly labeled and assigned a lower evidence level |
| MagicForge interpretation | A reviewer-approved synthesis, hypothesis, or mapping created by MagicForge; it must link its inputs and remain distinguishable from what a source states |

Cross-source agreement can raise confidence only through a documented review; it never changes the underlying evidence classes.

## State machines

### Current proposal/ingestion state machine

The implementation keeps its existing four statuses and transitions:

```text
                         named human approve
pending ------------------------------------------------> approved
   |                                                         |
   | named human reject                                      | separate explicit ingest action
   v                                                         v
rejected                                                  ingested

pending -- revise with same source candidate --> pending
```

Rules preserved from the current service:

1. Approval and rejection are valid only from `pending`.
2. A revision is valid only while `pending` and cannot replace the source candidate.
3. Approval requires a named human, non-empty notes, verified citation metadata, and content stronger than a search snippet.
4. Only an `approved` item may reach the ingestion adapter.
5. An item becomes `ingested` only if every expected projection is accepted. A partial Qdrant write leaves it `approved` for investigation and safe retry.

The new review fields refine what an approval event must record. They do not create an automated transition or a path from `pending`/`rejected` directly to Qdrant.

### Source-review decision semantics

Source review precedes the current extracted-proposal state machine:

```text
source pending -- approve scoped extraction --> source approved for extraction
       |
       +-- return / request information ------> source pending
       |
       +-- reject ----------------------------> source rejected (this version)
```

“Approved for extraction” is a source-scope decision, not the `approved` ingestion status of a `ReviewItem`. Implementations must keep these concepts in distinct fields or records.

### Return, hold, revision, and rejection

- **Return for revision** means the problem is repairable: missing locator, incomplete citation, over-broad claim, incorrect entity mapping, or insufficient notes. The current item remains `pending`; the revision and the reviewer request are appended to the audit log.
- **Hold** means an external dependency is unresolved, such as access rights, canonical edition, or a required specialist reviewer. It is represented by `pending` plus a documented blocker; it is not approval.
- **Reject** means the exact source version, claim, mapping, or bundle is unsuitable under the current protocol. Rejection is terminal for that reviewed object. Corrected material must be submitted as a new version linked to the rejected record; history is not overwritten.
- **Revoke/supersede after ingestion** requires a separately reviewed corrective version and a storage maintenance action. It must never silently mutate the original audit decision.

## Gate procedures

### G1 — Source and extraction review

The source reviewer checks:

1. Candidate identity, author, title, edition/version, date, stable URL/DOI, and citation verification evidence.
2. Access state and lawful processing scope. Availability on the web is not equivalent to permission to store text.
3. Whether the available material is metadata, abstract, extract, full text, or page-preserving PDF text.
4. Relevance to the research protocol and the appropriate evidence class.
5. Main concerns, known conflicts, and the scope proposed for extraction.
6. `Claim eligibility`, `Extraction permission`, `Storage permission`, and `Sensitive information level`.

G1 fails closed when the source is only a search snippet, its identity is unresolved, permission is unclear, or required metadata is unverified. A metadata-only approval may still support citation management but may not create content claims.

### G2 — Claim, Evidence Card, and mapping review

Claims are reviewed atomically. For every proposed Evidence Card, the claim reviewer must:

1. Open the source at the recorded page, section, paragraph, timestamp, or stable URL locator.
2. Confirm that the evidence excerpt supports the exact claim without changing modality, population, conditions, or causal strength.
3. Assign the evidence class and confidence using the Evidence Schema, including limitations.
4. Complete `Contradicting evidence checked` and link conflicts when present.
5. Distinguish a cognitive mechanism from a magic application, and a source statement from MagicForge interpretation.
6. Check the proposed applicable domains and avoid generalizing beyond the evidence.
7. Check entity identity and relation direction. `Source` and `ResearchPaper` nodes come from verified provenance, not GLM output.
8. Confirm sensitive method details have the appropriate exposure label and retrieval restrictions.

Cards may be approved, returned, or rejected independently. One rejected claim does not erase valid claims from the same source, and one approved claim does not validate the rest of the source.

### G3 — Storage authorization

The storage authorizer reviews an immutable manifest containing only approved artifacts. Authorization requires:

- all included Evidence Cards have human claim approvals;
- all included Knowledge Nodes and relations have valid provenance links;
- source and claim versions match the manifest;
- storage permission covers the proposed representation;
- sensitivity metadata and retrieval policy are present;
- no raw document text or temporary extraction chunk is included;
- the Qdrant payload schema validates; and
- a named ingestion actor is recorded separately from the approving reviewer.

The authorization applies only to the exact manifest hash. Any content, metadata, or relation change creates a new manifest and requires renewed authorization.

## Decision matrix

| Condition | Source extraction | Claim approval | Qdrant ingestion |
|---|---:|---:|---:|
| Search snippet only | No | No | No |
| Citation incomplete or invalid | No | No | No |
| Source approved but claim not reviewed | Yes, within scope | No | No |
| Practitioner statement presented as scientific evidence | May extract and label | Reject/return that classification | No |
| Claim supported but contradiction check incomplete | Already permitted | Remains pending | No |
| Claim approved but storage permission is `none` | Already permitted | Yes | No |
| Some items in a bundle are unapproved | Already permitted | Mixed | No for the bundle; create an all-approved manifest |
| Qdrant accepts fewer projections than expected | Already permitted | Unchanged | Do not mark `ingested`; keep `approved` and investigate/retry |

## Audit and reproducibility

Review records are append-only in meaning, even when the local adapter rewrites a JSON snapshot atomically. Every material action emits an event with:

- action and scope;
- named actor;
- UTC timestamp;
- required notes/reason;
- object ID and version/hash;
- prior event reference; and
- affected card, node, relation, or manifest IDs.

Reports should be able to answer: who approved this claim, on what source version, using which excerpt and locator, after which contradiction check, under what permissions, and which Qdrant points were produced.

## Minimum review checklists

### Source checklist

- [ ] Identity and citation verified against a primary or authoritative record
- [ ] Access state accurately recorded
- [ ] Extraction scope is lawful and explicit
- [ ] Evidence class does not overstate the source
- [ ] Claim eligibility decided
- [ ] Storage permission and sensitivity level recorded
- [ ] Named reviewer, date, and reason recorded

### Claim and mapping checklist

- [ ] Claim is atomic and faithful to the source
- [ ] Excerpt and locator independently verified
- [ ] Evidence class, confidence, limitations, and applicable domain recorded
- [ ] Contradicting evidence check completed and conflicts linked
- [ ] Scientific evidence, practitioner knowledge, and MagicForge interpretation remain separate
- [ ] Entity identity and relation direction verified
- [ ] Exposure level appropriate for method detail
- [ ] Named reviewer, date, and reason recorded

### Storage checklist

- [ ] Manifest includes only approved cards/nodes/relations
- [ ] Manifest versions and hash are fixed
- [ ] Storage permission covers every projection
- [ ] No raw PDF, raw chapter, or temporary source chunk is present
- [ ] Qdrant metadata validates and sensitivity filters are present
- [ ] Explicit storage authorization and ingestion actor recorded

## Relationship to existing artifacts

- `research/runs/.../review-queue.md` remains the source-candidate review queue. This specification adds columns/records but makes no decision on any current row.
- `SourceApprovalService` owns G1; `HumanReviewService`/`ClaimReviewService` owns atomic Evidence Card review; `MappingReviewService` owns domain mappings.
- `ApprovedKnowledgeIngestor` is the sole Qdrant transition and accepts an authorized Evidence Card/Knowledge Node projection manifest rather than raw source chunks.
- The extraction workflow and artifact/version rules are defined in [MagicForge Extraction Pipeline v0.1](extraction-pipeline-v0.1.md).
- Evidence Card fields and confidence semantics are defined in [MagicForge Evidence Schema v0.1](evidence-schema-v0.1.md).
