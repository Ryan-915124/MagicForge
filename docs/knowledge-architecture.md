# MagicForge Knowledge Architecture v0.1

MagicForge models reviewed claims and domain assertions rather than treating documents as knowledge. Qdrant stores rebuildable retrieval projections; no graph database is present.

## Canonical domain

Entities:

- `Effect`
- `Technique`
- `Method`
- `PsychologyPrinciple`
- `CognitiveMechanism`
- `Performer`
- `Source`
- `ResearchPaper`

Relations:

- `uses`
- `inspired_by`
- `requires`
- `explains`
- `performed_by`
- `related_to`

Canonical entities and relations keep deterministic UUIDs. `KnowledgeNodeVersion` and `KnowledgeRelationshipAssertion` add versioned definitions, limitations, confidence, human review metadata, and supporting/contradicting Evidence Card IDs. They can later be consumed by a graph adapter without changing Qdrant identities.

## Evidence layer

An `EvidenceCard` represents one source version and locator bearing on one atomic claim. It records:

- evidence class, evidence level, and knowledge origin;
- source/citation/version identity and minimal review excerpt;
- mechanism/principle links and applicable magic domains;
- extraction confidence separately from human evidence confidence;
- limitations, contradiction links, permissions, sensitivity, and exposure;
- a claim-level named-human decision.

Scientific evidence, expert practice, and personal interpretation cannot share evidence classifications or silently raise one another's confidence.

## Storage flow

```text
approved Evidence Cards
  -> approved Knowledge Node / Relationship assertions
  -> structured QdrantProjection
  -> immutable, human-authorized StorageManifest
  -> embedding
  -> Qdrant upsert and verification
  -> IngestionReceipt
```

`KnowledgeChunk` remains only as a legacy document-processing object. `QdrantService.add_documents()` and `knowledge.ingestion.ingest_paths()` fail closed. The only write contract is `ProjectionWriter.write_manifest(StorageManifest)`.

## Qdrant projection

Projection text is rendered in a fixed structure from approved claim/definition, origin, application/context, and limitations. The evidence review excerpt and raw source chunk are not embedding text.

Payload metadata includes routing, evidence, provenance, graph IDs, review status, storage permission, sensitive-information level, secret-exposure rank, artifact version, manifest ID/hash, and payload checksum. Retrieval always applies server-side governance filters before optional domain filters.

## Future graph integration

`KnowledgeNodeVersion`, `KnowledgeRelationshipAssertion`, stable entity/relation IDs, and Evidence Card provenance form the graph-neutral source model. A future graph adapter may persist the same objects and use Qdrant results as vector candidates. It must reuse the same human approvals, storage permissions, contradiction handling, and exposure policy; graph integration does not authorize re-extraction or new claims.
