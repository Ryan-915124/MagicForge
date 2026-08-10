# MagicForge Production Governance Backend

> Implementation addendum dated 2026-08-08, with an operational audit update
> dated 2026-08-09. This document records the backend
> implementation that follows the repository audit in
> `docs/MAGICFORGE_PROJECT_STATUS.md`; it does not replace or rewrite that
> audit. File and symbol names below refer to the repository state on this
> date.

## Current status

The Production governance chain is implemented through database-backed
application services and is exercised with isolated fixtures. A persistent
Production Qdrant corpus has **not** been written or activated. The default
Production target remains `magicforge_knowledge_v01`, but writes are disabled
by `PRODUCTION_QDRANT_WRITES_ENABLED=false` and a blank exact-write fingerprint
until a human reviews one manifest ID/hash, collection, point count, vector
shape, and activation procedure.

The authoritative Bootstrap corpus remains separate and unverified. Nothing in
this implementation imports, promotes, approves, or rewrites Bootstrap data.

## 1. Database architecture

Production persistence uses SQLAlchemy 2.x with PostgreSQL and Alembic. SQLite
is used only by isolated tests; it is not a Production fallback. The runtime
requires `DATABASE_URL` to resolve to PostgreSQL and verifies that the database
is at Alembic revision `20260808_0006` before exposing database-backed services.
The application does not call `create_all()` as a Production migration path.

The relational model consists of 29 tables in four migration groups:

| Area | Tables | Responsibility |
|---|---|---|
| Identity and transactions | `users`, `roles`, `user_roles`, `sessions`, `audit_events`, `idempotency_records` | Users, live role membership, revocable sessions, append-only audit, retry safety |
| Source, Claim, Evidence | `sources`, `source_versions`, `source_permission_requests`, `citation_verification_evidence`, `source_review_decisions`, `claim_candidates`, `claim_review_decisions`, `evidence_card_versions` | Immutable source content, bounded permission requests, review decisions, claim provenance, approved Evidence versions |
| Mapping | `mapping_proposals`, `mapping_proposal_evidence`, `mapping_validation_runs`, `mapping_review_decisions`, `knowledge_entities`, `knowledge_node_versions`, `knowledge_relationships`, `knowledge_relationship_assertions` | Reviewed ontology projection, canonical entities, versioned nodes and relationship assertions |
| Storage and corpus | `storage_manifests`, `storage_manifest_items`, `manifest_authorizations`, `ingestion_operations`, `ingestion_receipts`, `corpus_versions`, `active_corpus_pointers` | Exact projection sets, human authorization, Qdrant write saga, receipts, activation |

UUIDs identify relational and domain records. Timestamps are UTC. JSON/JSONB is
used for validated domain payloads and snapshots while searchable workflow,
identity, status, version, actor, and checksum fields remain normalized and
indexed. Foreign keys, uniqueness checks, workflow checks, optimistic row
versions, and PostgreSQL triggers reinforce application-layer invariants.

`persistence.uow.SqlAlchemyUnitOfWork` owns the transaction boundary. Domain
mutation, state transition, decision row, audit event, and idempotency result
are committed together. An exception before commit rolls back the relational
unit of work.

Relevant implementation:

- `persistence/models.py`
- `persistence/storage_models.py`
- `persistence/database.py`
- `persistence/uow.py`
- `migrations/versions/20260807_0001_transactional_foundation.py`
- `migrations/versions/20260807_0002_source_claim_workflow.py`
- `migrations/versions/20260808_0003_mapping_workflow.py`
- `migrations/versions/20260808_0004_storage_workflow.py`
- `migrations/versions/20260808_0005_align_mapping_workflow_enum_width.py`
- `migrations/versions/20260808_0006_source_permission_requests.py`

## 2. Authentication and session model

MagicForge has no public registration endpoint and no built-in password. The
first administrator can only be created explicitly with
`python -m security.admin_cli create-initial-admin`. The CLI reads a password
from a hidden prompt or an explicitly named environment variable; it does not
accept the password as a command-line argument and does not echo it.

Passwords use Argon2id with explicit cost parameters. Sessions use independent
256-bit opaque random tokens. Only SHA-256 token digests are persisted, and
digest comparisons use constant-time comparison. Session records are
revocable, expire at an absolute timestamp, track a bounded `last_used_at`, and
are invalidated when the user is disabled or the password is newer than the
session.

Two transports are supported:

- `cookie`: the session token is `HttpOnly`; mutations require an exact allowed
  `Origin`, a double-submit CSRF value, and verification against the persisted
  CSRF-token digest. Production cookies default to `Secure` and `SameSite=Lax`.
- `bearer`: an opaque bearer session for non-browser clients. CSRF is not
  applied because the browser does not attach this credential implicitly.

Authentication errors use stable public codes. Raw tokens, password hashes,
database driver details, and credential-bearing URLs are not included in API
responses or audit payloads.

Relevant implementation:

- `security/crypto.py`
- `security/services.py`
- `security/admin_cli.py`
- `app/auth_routes.py`
- `app/security_runtime.py`

## 3. RBAC matrix

Roles are additive, but `admin` is deliberately not an implicit reviewer or
operator. Role checks occur at the FastAPI dependency boundary and are rerun
inside mutation services against the current database session, user state, and
live role assignments.

| Capability | reader | reviewer | operator | admin |
|---|:---:|:---:|:---:|:---:|
| Product read / Chat / inspection | Yes | Yes | Yes | Yes |
| Research Console | No | No | Yes | Yes |
| Register Source / new Source version | No | No | Yes | No |
| Submit Claim | No | No | Yes | No |
| Submit Mapping proposal | No | No | Yes | No |
| Review Source | No | Yes | No | No |
| Review Claim | No | Yes | No | No |
| Review Mapping | No | Yes | No | No |
| Build / authorize / ingest manifest | No | No | Yes | No |
| Activate corpus | No | No | Yes | No |
| Create users / change roles / revoke sessions | No | No | No | Yes |
| Read bounded audit catalogue | No | No | No | Yes |
| Break-glass capability | No | No | No | Policy primitive only; no HTTP activation flow yet |

Reviewers cannot approve their own Source, Claim, or Mapping submissions.
Source and Claim violations return a state conflict; Mapping rejects the
operation through the governance authorization boundary. Storage authorization,
ingestion, and activation require a named human operator identity; known
automation identities are rejected. The current policy does not require the
manifest builder and authorizer to be different operators.

Retrieval clearance is derived server-side:

- ordinary authenticated users: `PUBLIC` and `CONTROLLED`, up to general
  principles;
- reviewer: additionally `SECRET_METHOD`, up to method detail;
- explicitly elevated admin break-glass actor: additionally `RESTRICTED`, up
  to operational secret;
- explicitly enabled Bootstrap anonymous principal: Bootstrap-only,
  read-only, `PUBLIC` and `CONTROLLED` general principles.

Clients cannot submit roles, clearance, or allowed sensitivity levels as a
trusted retrieval authorization object.

## 4. Audit event model

`audit_events` records:

- event ID and event type;
- authenticated actor user ID and role snapshot;
- object type and object ID;
- domain schema version and optional payload checksum;
- previous state and new state;
- required human or operation reason;
- request/correlation IDs;
- bounded metadata and UTC creation timestamp.

`AuditEventRepository.append()` rejects credential-like metadata keys and
requires JSON-safe state snapshots. The audit insert is flushed inside the
same transaction as its governance mutation; if audit persistence fails, the
domain mutation rolls back.

Audit rows are insert/query-only in the repository. ORM event guards reject
updates and deletes in tests and application code, while the PostgreSQL
migration installs an append-only trigger. Source review decisions, Claim
review decisions, Mapping review decisions, validation runs, manifest items,
manifest authorizations, and ingestion receipts likewise preserve historical
records rather than rewriting them.

`GET /audit/events` exposes a bounded, administrator-only audit catalogue. It
reruns `AUDIT_READ` against the live database session and roles, supports exact
actor/event/object/request/correlation filters plus an aware UTC time range,
and uses a newest-first keyset cursor bound to the active filter set. The list
DTO returns identifiers, actor identity, checksums, and state field names. It
deliberately omits the free-text custody reason and does not bulk-serialize
`previous_state`, `new_state`, or metadata values. Full reasons remain in the
immutable database record for a future clearance-aware detail/export workflow.
Responses are marked `Cache-Control: no-store`.

Incoming request and correlation headers are admitted only when they match the
bounded trace-ID alphabet (`A-Z`, `a-z`, digits, `.`, `_`, `:`, `-`); arbitrary
header text is discarded before it can enter an audit event.

## 5. Source workflow

The implemented Source path is:

```text
operator registers canonical Source
  -> immutable Source version #1 (submitted)
  -> reviewer inspects exact version and verification evidence
  -> approved or rejected decision + audit event
  -> optional later immutable Source version, never an overwrite
```

A Source version stores canonical citation metadata, content and content hash,
source category/type, `knowledge_origin`, access and rights metadata,
requested extraction/storage permissions, scope locators, sensitivity, schema
version, submitter, and timestamps. The canonical Source identity cannot change
across versions. Duplicate content for the same Source is rejected.

Citation status is derived from immutable verification evidence rather than a
caller-supplied `verified` flag. A verification observation records scope,
method, resolver outcome, verified identifier, checked locator, resolver name,
reviewer, timestamp, and checksum. Full-text verification requires the content
checksum method. Missing or non-matching evidence does not produce verified
status.

Approval is allowed only when citation, access, extraction, storage,
eligibility, sensitivity, and contradiction-check requirements form a valid
review decision. A reviewer cannot review their own submission. Route handlers
do not assign workflow status directly; `ProductionGovernanceService` owns the
transition and transaction.

`GET /review/sources` exposes a lightweight version-level queue for reviewers.
It returns identifiers, title, type/origin, access and citation state, current
workflow state, sensitivity, and timestamps, but never raw Source content. The
service computes effective sensitivity and applies visibility and status
filters before offset/limit pagination. Raw citation, access metadata, and
content remain available only through the clearance-checked Source detail.

## 6. Claim and Evidence workflow

A Claim candidate references one exact, currently approved Source version and
contains Claim text, `claim_role`, polarity, locator, extraction provenance,
model/run metadata where applicable, proposed Evidence class, extraction
confidence, schema version, checksum, sensitivity context, and submitter type
(`human`, `glm`, or `pipeline`). Machine origin is provenance, not approval.

Evidence class is validated against `claim_role`, not inferred from Source
type. Scientific evidence, expert practice, and personal interpretation remain
separate knowledge origins. In particular:

- `context_only` cannot produce an Evidence Card;
- background material cannot be promoted to a controlled experiment;
- hypotheses cannot become empirical evidence;
- discussion claims must retain uncertainty;
- practitioner sources cannot claim scientific-evidence origin.

A named reviewer approves or rejects the candidate after checking the exact
Source decision, locator, evidence class, confidence, limitations,
contradiction state, storage eligibility, and sensitivity. On a valid approval,
the Claim decision and an immutable `EvidenceCard` v0.2 version are created in
the same transaction and linked by ID. An audit failure rolls back both.

`GET /review/claims` uses deterministic, bounded SQL windows and applies the
same effective Source-plus-Claim sensitivity rule as Claim detail before an
item enters the logical offset/limit page. A lower-sensitivity sibling Claim
cannot expose a higher-sensitivity version. Reaching the scan ceiling produces
an explicit fail-closed response rather than a short page with hidden holes.

Claim review detail includes the exact Source-version context needed for a
decision: Source/version IDs, title, citation, access metadata, content,
origin/type, citation state, and effective sensitivity. This context is
returned only after both Claim and Source clearance checks pass; it is not part
of the lightweight Claim queue.

The Product Evidence read path applies actor authorization before returning a
DTO. The Production read model only hydrates exact Evidence versions in the
active manifest, rechecks payload checksums, current Claim approval, current
Source approval, effective Source sensitivity, and latest Evidence version,
and suppresses source locator or
excerpt content when the approved storage permission does not allow that
material. `GET /evidence/{identifier}/versions` accepts a family/domain
identifier for version history, but an exact `EvidenceCardVersion.id` has
strict precedence and returns only that SQL row; an exact mapping dependency
can therefore never broaden into family history.

## 7. Mapping workflow

The Mapping workflow accepts two proposal kinds:

- entity proposal -> canonical `KnowledgeEntity` plus a versioned
  `KnowledgeNodeVersion`;
- relationship proposal -> canonical endpoint pair plus a versioned
  `KnowledgeRelationshipAssertion`.

Each proposal contains exact supporting Evidence-version references, domain
and dot-notation ontology paths, knowledge origin, evidence excerpt and
locator, limitations, extraction confidence, proposer identity/run, schema
version, and proposal checksum. Chinese aliases, translated labels, and display
labels are never sufficient as canonical identity keys.

Before review, the service reloads all supporting Evidence and requires current
Source and Claim approval and the latest Evidence version. Entity validation
enforces ontology semantics such as audience-perceived Effects, executable
Techniques, secret Methods, person/group Performers, and scientifically valid
`CognitiveMechanism` entities. Canonical resolution explicitly chooses
`create`, `reuse`, or `merge`; duplicates are not silently copied. Ontology
paths use dot notation.

Relationship approval reruns endpoint/type and deterministic entailment rules.
`performed_by`, `requires`, `explains`, and `uses` retain their typed semantic
requirements; co-mention, correlation, or experimental stimulus alone is not
accepted as necessity or use. `related_to` remains a fallback relation.

Validation results and rule versions are persisted in
`mapping_validation_runs`. A different reviewer from the proposer must make
the final decision. Approval atomically writes the decision, canonical entity
change where needed, node/relationship version, and audit event.

Every approved Node or Relationship remains bound to the exact
`MappingProposalEvidence` rows used for review. Storage compares the proposal's
Evidence-version IDs, link-table row IDs, domain IDs, and immutable payload
checksums. Substituting a sibling Evidence version is rejected even if its
family or wording is compatible. Node and Relationship detail reuse the same
exact current-dependency gate rather than trusting only the artifact envelope.

`GET /knowledge/entities` queries only the active SQL canonical registry for
Mapping review. It supports canonical name/key/alias search and entity-type
filtering, applies current sensitivity clearance before pagination, and
returns the latest approved Knowledge Node row/version for each result. It is
not backed by Qdrant and cannot return merged or inactive identities.

## 8. Manifest authorization

Manifest construction and authorization are separate operations. A client may
submit only corpus/collection identity, exact approved artifact row IDs, and a
requested sensitivity set. It cannot supply trusted projection JSON, review
status, checksums, or authorization identity.

Operators discover those exact inputs through
`GET /storage/eligible-artifacts`. The response is derived by running the same
current-approval, checksum, ontology, relationship, sensitivity, storage-policy,
and projection gates used for manifest construction. Each result includes the
SQL row ID, domain ID, version, payload checksum, sensitivity, short subject,
and exact supporting Evidence row IDs. Candidate discovery uses a stable SQL
`UNION ALL` ordering over only the latest immutable version in each artifact
family, followed by bulk row loading and 128-item governance-resolution
batches. Exact Mapping Evidence links are loaded per batch rather than per
artifact. The bounded catalogue snapshot supports up to 10,000 candidates and
offsets through 9,900, which covers the planned 1,000+ Evidence corpus without
an unbounded full-table materialization. A malformed or revoked member is
isolated by fail-closed batch bisection, so valid neighbours still fill the
logical page. If the ceiling prevents proving a complete page, the service
fails explicitly instead of returning a hidden page hole. Manifest summaries can
be paged and filtered by status/corpus through `GET /storage/manifests`; exact
ordered items remain on the manifest-detail endpoint.

During construction the server:

1. resolves every exact Evidence, Knowledge Node, and Relationship Assertion
   version from SQL;
2. rechecks that it is the latest approved, non-stale version;
3. verifies stored domain payload checksums;
4. reruns Evidence eligibility, entity validation, relationship entailment,
   sensitivity, and schema gates;
5. builds a canonical `manifest-0.2` over `qdrant-0.2` projections;
6. persists immutable ordered items with both domain-payload and projection
   checksums.

The manifest is initially `pending`. Authorization requires an operator and a
reason. The service reconstructs the manifest from its exact SQL items and
reruns all gates at authorization time. The rebuilt identity and SHA-256 hash
must match the pending manifest. Authorization then persists the named user ID,
role snapshot, reason, timestamp, validation snapshot, and audit event.

Manifest detail, list entries, and completed build/authorization idempotency
replays are not historical-visibility shortcuts. Before returning them, the
service derives current clearance, validates the stored manifest/item envelope,
and reruns current Source/Claim/Mapping approval and projection reconciliation.
Inaccessible or stale list rows are never serialized; detail and replay fail
closed.

Only `PUBLIC`/`CONTROLLED` content is practical under the current operator
retrieval clearance. A requested sensitivity set must be a subset of the
operator's server-derived clearance and every item must belong to that set.

## 9. Qdrant ingestion

Raw document/chunk ingestion is disabled. `QdrantService.add_documents()`
fails closed; the writer accepts only an authorized `StorageManifest`.

The ingestion boundary is:

```text
authorized SQL manifest
  -> committed running ingestion operation
  -> exact collection/corpus/manifest-bound Qdrant points
  -> point and payload verification
  -> review_status promotion from approved staging to ingested
  -> immutable SQL receipt + staged corpus version + audit event
```

Every point carries the corpus ID, storage manifest ID/hash, projection schema,
payload checksum, governance flags, provenance, evidence references,
sensitivity, and secret-exposure metadata. The collection must use the
configured vector dimension and Cosine distance. An existing collection is
accepted only when it is exclusively and exactly bound to the same manifest;
extra points or another identity fail closed.

The receipt binds manifest, corpus, collection, ordered point IDs, payload
checksums, actor, timestamp, and success. A repeated completed ingestion with
the same actor/key/request returns the existing receipt. A changed payload with
the same key is rejected.

Immediately before any ingestion operation row is created, the service reloads
the manifest's exact SQL rows, reruns all current approval and eligibility
gates, rebuilds the canonical manifest, and compares manifest identity, hash,
point IDs, and projection checksums. A post-authorization revocation, policy
change, supersession, canonical-entity change, or payload mismatch therefore
fails before a Qdrant writer can be invoked.

The same reconciliation runs again after the external writer returns and while
the final SQL transaction holds the operation and manifest locks. Only then may
a receipt and staged corpus be committed. If approval, clearance, canonical
identity, an artifact/projection checksum, or the manifest hash changed during
the write, finalization fails and manifest-owned points are cleaned. When safe
ownership cannot be proven, the operation remains `reconciliation_required`.

Persistent writes are off by default. The normal runtime constructs the writer
with temporary writes disabled and uses
`PRODUCTION_QDRANT_WRITES_ENABLED=false`. That switch alone grants no write:
`PRODUCTION_QDRANT_WRITE_MANIFEST_ID`,
`PRODUCTION_QDRANT_WRITE_MANIFEST_HASH`,
`PRODUCTION_QDRANT_WRITE_COLLECTION`, and
`PRODUCTION_QDRANT_WRITE_POINT_COUNT` must all be present and exactly match the
authorized manifest at write start and finalization. Partial or mismatched
fingerprints fail closed. Isolated tests explicitly enable only
collections prefixed `magicforge_cp4_test_`. Any name containing `bootstrap`
is rejected as a Production storage target.

## 10. Production corpus activation

Successful ingestion creates a `CorpusVersionRecord` in `staged` state. It
contains corpus and manifest IDs, receipt ID, Qdrant collection, manifest and
projection schema versions, vector size/distance, creator, and timestamp.

Activation is a separate operator action with its own reason, idempotency key,
row locks, audit event, and transaction. The service requires an ingested
manifest, matching receipt, matching corpus identity, and no unresolved
reconciliation operation. It marks the selected corpus `active`, the previous
corpus for the same scope `inactive`, and updates the versioned
`active_corpus_pointers` row. Database indexes enforce one active corpus per
runtime scope.

At startup, Production loads the pointer for `production` and verifies the
corpus, manifest, authorization, receipt, items, checksums, collection,
`manifest-0.2`, `qdrant-0.2`, vector distance, and configured identity. It then
constructs one immutable `ActiveCorpus` shared by Chat, Knowledge/Evidence
browser reads, the retriever, and the Production Research Console adapter.

The Production Knowledge reader hydrates only exact artifact versions listed
in that manifest. It reruns current approval/latest-version checks and applies
actor-bound sensitivity/secret filtering. Relationships are returned only when
both endpoint entities are visible.

The process does not trust that startup state forever. Before Production
health/readiness, Chat retrieval, or browser reads use cached services, the
reader rechecks the live corpus pointer, corpus state, receipt checksum,
manifest identity, current Source/Claim/Mapping decisions, and canonical entity
state. A corpus switch, revocation, merge, or policy tightening immediately
fails closed with `active_corpus_stale`; a new runtime must then be constructed.

No persistent Production corpus currently exists in this repository state, so
Production product endpoints intentionally remain not-ready until a separately
approved ingestion and activation are completed.

## 11. Bootstrap / Production isolation

Isolation is explicit at configuration, service composition, storage, and read
boundaries:

- `MAGICFORGE_MODE=production` requires PostgreSQL and a database-backed active
  Production corpus; it never opens Bootstrap run files as a fallback.
- Production requires remote Qdrant and rejects collection names containing
  `bootstrap`.
- `build_storage_workflow_service()` returns no writer service in Bootstrap
  mode.
- Qdrant Production mutation refuses Bootstrap mode and Bootstrap collection
  names.
- Production readiness requires `bootstrap_generated=false` and
  `human_verified=true` throughout manifest/payload identity checks.
- Bootstrap anonymous reads exist only when both
  `MAGICFORGE_MODE=bootstrap` and `BOOTSTRAP_ALLOW_ANONYMOUS_READS=true`; they
  receive no mutation permission.
- Bootstrap artifacts, manifests, receipts, and `magicforge_bootstrap_v03`
  remain read-only and unverified.

There is no automatic import, migration, promotion, human approval, or
Production projection of `research/runs/bootstrap-001/` or
`research/runs/bootstrap-002/`.

## 12. Liveness and readiness

The API exposes three related probes:

| Endpoint | Meaning |
|---|---|
| `GET /health/live` | Process is alive. It does not prove database, corpus, Qdrant, or GLM connectivity. |
| `GET /health/ready` | Database policy is satisfied and one complete runtime has passed active-corpus and Qdrant readiness checks. Returns stable `503` details on failure. |
| `GET /health` | Compatibility health response with mode, corpus, schema, manifest, collection/storage kind, and whether GLM is configured. It does not call GLM. |

Database initialization occurs in the FastAPI lifespan. Production fails
readiness when `DATABASE_URL` is absent, PostgreSQL is unreachable, or the
Alembic head differs from `20260808_0006`. Runtime construction occurs after
database initialization and fails closed when no authorized active Production
corpus exists, the receipt/manifest identity is inconsistent, Qdrant is absent,
the vector shape is wrong, or point counts/identity filters do not match.

`import app.main` and `create_app()` construct configuration and lifecycle
objects but do not intentionally open PostgreSQL or Qdrant; external resources
are opened in lifespan/runtime startup.

## 13. API routes

All Production product reads require an authenticated role with
`PRODUCT_READ`. Governance and storage mutations also require a live session,
service-level RBAC, and (for cookie sessions) CSRF/Origin validation.

| Route | Purpose | Effective permission |
|---|---|---|
| `POST /auth/login` | Create cookie or bearer session | Public credential check |
| `POST /auth/logout` | Revoke current session | Authenticated session |
| `GET /auth/me` | Return current actor and roles | Authenticated session |
| `POST /admin/users` | Create a user; no public registration | `USER_ADMIN` |
| `PATCH /admin/users/{user_id}/roles` | Grant or revoke role | `ROLE_ADMIN` |
| `POST /admin/sessions/{session_id}/revoke` | Revoke session | `SESSION_REVOKE` |
| `GET /audit/events` | Filtered, keyset-paginated immutable audit catalogue | `AUDIT_READ` |
| `POST /sources` | Register Source and v1 | `SOURCE_REGISTER` |
| `POST /sources/{source_id}/versions` | Add immutable Source version | `SOURCE_REGISTER` |
| `GET /review/sources` | Lightweight Source-version review queue; filters before pagination | `SOURCE_REVIEW` |
| `GET /sources/{source_id}` | Reviewer detail | `SOURCE_REVIEW` |
| `POST /sources/{source_id}/review` | Approve/reject exact Source version | `SOURCE_REVIEW` |
| `POST /claims` | Submit checksummed Claim | `CLAIM_SUBMIT` |
| `GET /review/claims` | Paginated review queue | `CLAIM_REVIEW` |
| `GET /claims/{claim_id}` | Claim review detail | `CLAIM_REVIEW` |
| `POST /claims/{claim_id}/review` | Approve/reject; may create Evidence version | `CLAIM_REVIEW` |
| `GET /evidence/{identifier}/versions` | Version history subject to sensitivity | `PRODUCT_READ` |
| `POST /mappings/entities` | Submit entity Mapping | `MAPPING_SUBMIT` |
| `POST /mappings/relationships` | Submit relationship Mapping | `MAPPING_SUBMIT` |
| `GET /review/mappings` | Mapping review queue | `MAPPING_REVIEW` |
| `GET /mappings/{mapping_id}` | Mapping review detail | `MAPPING_REVIEW` |
| `POST /mappings/{mapping_id}/review` | Approve/reject Mapping | `MAPPING_REVIEW` |
| `GET /knowledge/entities` | Search active SQL canonical entities for Mapping resolution | `MAPPING_REVIEW` |
| `GET /knowledge/versions/{version_id}` | Reviewed node-version detail | `MAPPING_REVIEW` |
| `GET /knowledge/relationship-assertions/{assertion_id}` | Assertion detail | `MAPPING_REVIEW` |
| `GET /storage/eligible-artifacts` | Discover exact currently eligible artifact row IDs | `MANIFEST_BUILD` |
| `POST /storage/manifests` | Build exact manifest | `MANIFEST_BUILD` |
| `GET /storage/manifests` | Paginated manifest summaries, filterable by status/corpus | Any manifest operation permission |
| `GET /storage/manifests/{manifest_id}` | Inspect storage manifest | Any manifest operation permission |
| `POST /storage/manifests/{manifest_id}/authorize` | Human storage authorization | `MANIFEST_AUTHORIZE` |
| `POST /storage/manifests/{manifest_id}/ingest` | Execute governed Qdrant write | `MANIFEST_INGEST` |
| `GET /corpora` | List corpus versions | `PRODUCT_READ` or corpus operation permission |
| `GET /corpora/active` | Inspect active corpus | `PRODUCT_READ` or corpus operation permission |
| `POST /corpora/{corpus_id}/activate` | Activate staged corpus | `CORPUS_ACTIVATE` |
| `POST /assistant` | RAG answer through GLM | `PRODUCT_READ` |
| `POST /analyze` | Structured Magic Theory analysis | `PRODUCT_READ` |
| `POST /create` | RAG-assisted creation | `PRODUCT_READ` |
| `GET /knowledge/search` | Authorized Knowledge/Evidence search | `PRODUCT_READ` |
| `GET /knowledge/node/{identifier}` | Authorized node read | `PRODUCT_READ` |
| `GET /evidence/{identifier}` | Authorized Evidence read | `PRODUCT_READ` |
| `GET /stats` | Active corpus statistics | `PRODUCT_READ` |
| `GET /research/console` | Runtime/research snapshot | `RESEARCH_CONSOLE` |

Mutation requests for Source, Claim, Mapping, manifest, ingestion, and corpus
activation require `Idempotency-Key`. The current backend does not expose a
decision revocation endpoint, password-change endpoint, or user-disable
endpoint.

## 14. State transitions

State changes are performed in services under row locks, not by route code.
Invalid transitions return `409`.

```text
Session:
  active -> revoked
  active -> expired

Source version decision:
  submitted -> approved | rejected

Claim candidate:
  submitted -> approved | rejected
  approved -> immutable Evidence Card version (same transaction)

Mapping proposal:
  submitted -> approved | rejected
  approved -> immutable Knowledge Node version
            | immutable Relationship Assertion version

Storage manifest:
  pending -> authorized -> ingested

Ingestion operation:
  running -> succeeded
          -> failed_cleaned
          -> reconciliation_required

Corpus version:
  staged -> active -> inactive
```

The persistence enums also reserve `superseded` and `revoked` governance
states, and decision records can represent correction history. The current HTTP
surface only implements submit plus approve/reject for Source, Claim, and
Mapping; revocation/supersession operations are a remaining milestone.

## 15. Idempotency behavior

Governed mutation keys are scoped by authenticated user and operation. The
server hashes a canonical JSON request representation and stores the key,
scope, actor, request hash, workflow status, response status/body, expiry, and
timestamps.

- first request: acquires an `in_progress` record in the same transaction;
- identical retry after completion: returns the stored result without a second
  domain mutation or approval;
- same key with different payload: `409 idempotency_conflict`;
- concurrent/in-progress key: stable conflict rather than duplicate work.

PostgreSQL uses a savepoint around the unique-key acquisition so a concurrent
winner does not poison the outer transaction. Idempotency does not turn an
unauthorized request into an authorized one: every replay path verifies the
current actor and resource visibility first.

Qdrant ingestion also has a dedicated operation envelope. A succeeded request
returns its receipt; a running or reconciliation-required operation is not
silently retried. A database partial unique index permits at most one
`running` or `reconciliation_required` operation per manifest, including when
different operators use different idempotency keys.

## 16. Failure and rollback behavior

Relational workflow operations are atomic. Examples:

- if an audit insert fails, Source/Claim/Mapping/authorization/activation state
  is rolled back;
- if an immutable child row or checksum validation fails, the manifest is not
  authorized;
- if Claim approval cannot form a valid Evidence Card, neither approval nor
  Evidence version is committed;
- if mapping validation fails, no approved node/assertion is produced;
- if activation validation fails, the active pointer is unchanged.

PostgreSQL and Qdrant cannot share one ACID transaction, so ingestion uses a
saga:

1. persist and audit `running`;
2. write and verify the exact manifest in Qdrant;
3. on success, atomically persist receipt, mark manifest ingested, create staged
   corpus, finish operation, and audit;
4. on failure, delete only points whose corpus/manifest/hash/checksum ownership
   exactly matches the failed manifest;
5. if ownership is ambiguous or cleanup cannot be proved, retain the data and
   mark `reconciliation_required` rather than risk deleting another corpus.

A failed or unresolved ingestion never creates an activatable state. There is
currently no automated reconciliation worker; operator investigation is
required.

## 17. Local development setup

Prerequisites are Python 3.11+, the repository virtual environment,
PostgreSQL, and Qdrant when testing a ready read runtime. Use `.env.example` as
the variable inventory; do not commit `.env` or reuse the local development
database password outside the provided container.

Start the provided local PostgreSQL service:

```bash
docker compose -f docker-compose.dev.yml up -d postgres
```

Install dependencies and migrate:

```bash
python -m pip install -r requirements.txt
python -m alembic upgrade head
```

Create the first administrator with a hidden prompt:

```bash
python -m security.admin_cli create-initial-admin --username <admin-name>
```

Then start the API:

```bash
uvicorn app.main:app --reload
```

In Production mode, `/health/live` can be alive while `/health/ready` is `503`
until an authorized Production corpus is ingested and activated. That is the
expected fail-closed state, not a reason to point Production at Bootstrap.

For frozen local Bootstrap inspection, use the explicit Bootstrap settings
shown in `.env.example`. Anonymous reads are disabled unless deliberately
enabled and never grant governance mutation permissions.

## 18. Migration commands

Alembic reads `DATABASE_URL` and rejects non-PostgreSQL targets.

```bash
# Show current database revision
python -m alembic current

# Show migration chain
python -m alembic history

# Upgrade an empty or existing compatible database
python -m alembic upgrade head

# Generate SQL without connecting (with DATABASE_URL set to a non-secret
# PostgreSQL target appropriate for the environment)
python -m alembic upgrade head --sql
```

The migration chain is:

```text
20260807_0001_transactional_foundation
  -> 20260807_0002_source_claim_workflow
  -> 20260808_0003_mapping_workflow
  -> 20260808_0004_storage_workflow
  -> 20260808_0005_align_mapping_workflow_enum_width
  -> 20260808_0006_source_permission_requests
```

Each migration has a downgrade implementation. Downgrading governance tables
is destructive to their history, so it should only be done in an isolated
environment or after a verified backup. Application startup never performs a
destructive reset or auto-import of file artifacts. A partially failed Alembic
migration relies on PostgreSQL transactional DDL and should be diagnosed before
retrying; do not drop the schema as an automatic recovery strategy.

## 19. Security boundaries

The implemented security boundaries are:

- no public registration and no default credentials;
- Argon2id password hashing and opaque, hashed, revocable sessions;
- exact cookie Origin allowlist and persisted double-submit CSRF validation;
- stable `401`, `403`, `404`, `409`, `422`, and `503` boundaries without raw
  backend exception details;
- database-live RBAC inside mutation services, not only UI or route checks;
- self-review prevention for Source, Claim, and Mapping;
- named-human requirements for review and storage authority;
- actor-derived retrieval authorization and pre-serialization filtering;
- sensitive/secret content is filtered before it reaches response DTOs or GLM
  context;
- immutable/checksummed versions and append-only decisions/audit records;
- no raw PDF/chunk-to-Qdrant path;
- manifest-, collection-, corpus-, schema-, checksum-, and receipt-bound Qdrant
  reads and writes;
- current approval and canonical manifest identity are rebuilt before an
  ingestion operation and again before receipt finalization, so stale or
  mid-write-revoked authorization cannot become an activatable corpus;
- exact active point IDs and payload checksums are independently revalidated;
  returned search points are locally rechecked against actor sensitivity,
  clearance, and caller filters before entering API output or GLM context;
- Production Qdrant mutation capability is disabled in the writer itself by
  default; the workflow additionally requires both the global switch and one
  exact manifest/hash/collection/point-count capability;
- Production does not fall back to Bootstrap or anonymous controlled access;
- configuration and audit representations redact or reject secrets.

Operational requirements outside the repository still apply: terminate TLS at
a trusted proxy, use a secret manager for credentials, narrow CORS/origins,
restrict PostgreSQL/Qdrant network access, rotate the previously exposed Z.AI
credential, run with least-privilege database users, and monitor audit and
authentication events.

## 20. Remaining limitations

The backend governance architecture is implemented, but the deployment is not
yet fully Production-ready:

1. No persistent Production manifest has been approved, written, or activated;
   exact Production point counts and manifest identity therefore do not yet
   exist.
2. `PRODUCTION_QDRANT_WRITES_ENABLED` and the exact write-fingerprint fields
   remain disabled/blank. A separate human review must confirm the manifest ID,
   SHA-256 hash, collection, and point count before they are configured.
3. Empty-database migration integration was verified against a disposable
   PostgreSQL instance on 2026-08-08. CI still needs its own disposable
   PostgreSQL service and `TEST_DATABASE_URL` to preserve that coverage.
4. Backup, restore, disaster recovery, retention, and audit-export procedures
   are not implemented.
5. `GET /audit/events` is intentionally a safe catalogue rather than a raw
   state dump. Detailed audit-state inspection, signed export, retention, and
   external archival remain future operational work.
6. Revocation, supersession, correction, and reopening APIs for approved
   Source/Claim/Mapping decisions are not yet exposed.
7. The same operator may currently build, authorize, ingest, and activate a
   corpus; four-eyes separation is not enforced.
8. Break-glass policy primitives exist, but there is no complete audited HTTP
   elevation/expiry workflow.
9. No MFA, distributed login rate limiting, durable login lockout,
   password-reset/change endpoint, or centralized security-event alerting is
   implemented. The Next.js BFF has only a bounded, per-process,
   per-identifier throttle as a first-line safeguard.
10. Qdrant/SQL coordination is a compensating saga, not a distributed
    transaction; reconciliation is manual when safe cleanup cannot be proven.
11. Production Research Console data remains intentionally unavailable until
    a Production-specific governed snapshot contract is implemented.
12. The strict projection gate rejects cases where a purported claim is merely
    the source excerpt; legacy exact-quote candidates may require a corrected,
    reviewed version before they can enter a Production manifest.
13. Secure deployment configuration, user-created operational credentials,
    network policy, monitoring, and capacity/load validation remain external
    milestones.
14. The Next.js BFF now forwards only approved auth/mutation headers and
    whitelisted session/CSRF cookies, implements Production login/logout, and
    forces authenticated responses to `no-store`. A trusted reverse proxy,
    TLS, distributed rate limiting, and trustworthy client-IP attribution are
    still deployment requirements.
15. `/governance` provides reviewer Source/Claim/Mapping queues and an operator
    Manifest/Corpus Release Desk. It intentionally has no ingestion route or
    button; persistent ingestion remains an out-of-band operator operation
    protected by the exact write fingerprint and separate confirmation.
16. Historical Evidence rows do not yet contain a direct foreign key to the
   exact Source review decision that was current at creation time. Build,
   authorization, ingestion, and read gates compensate by comparing the latest
   Source and Claim policies with the immutable card and failing closed on any
   mismatch; a future migration should make that provenance link explicit.

## Architecture diagram

```text
                         MagicForge clients
                                |
                                v
                    +------------------------+
                    | FastAPI route boundary |
                    | validation + CSRF      |
                    +-----------+------------+
                                |
                                v
                    +------------------------+
                    | live session + RBAC    |
                    | actor-derived clearance|
                    +-----------+------------+
                                |
              +-----------------+------------------+
              |                                    |
              v                                    v
  +---------------------------+       +---------------------------+
  | Governance write services |       | Product read/runtime      |
  | Source -> Claim/Evidence   |       | one immutable ActiveCorpus|
  | -> Mapping -> Manifest     |       +-------------+-------------+
  | -> Ingest -> Activate      |                     |
  +-------------+-------------+                     |
                |                                   |
                v                                   |
  +---------------------------+                     |
  | SQLAlchemy Unit of Work   |                     |
  | state + decision + audit  |                     |
  | + idempotency atomically  |                     |
  +-------------+-------------+                     |
                |                                   |
                v                                   v
  +---------------------------+       +---------------------------+
  | PostgreSQL                |       | Qdrant                    |
  | canonical versions,       |<----->| manifest/corpus-bound     |
  | authorization, receipt,   | verify| structured projections    |
  | corpus pointer            |       | only                      |
  +-------------+-------------+       +-------------+-------------+
                |                                   |
                +-----------------+-----------------+
                                  |
                                  v
                    +-----------------------------+
                    | Production read model       |
                    | exact manifest versions,    |
                    | checksum/staleness/security |
                    +-------------+---------------+
                                  |
                       +----------+----------+
                       |                     |
                       v                     v
                 Chat / Analyze       Knowledge / Evidence
                 / Create             Search / Stats

  Bootstrap runtime is a separate read-only branch. It is never an input to
  the Production SQL -> Manifest -> Qdrant -> ActiveCorpus path.
```
