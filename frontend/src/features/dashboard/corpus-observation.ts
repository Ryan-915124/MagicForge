import type { CorpusStatsResponse } from "@/lib/api/types"

export interface CorpusObservation {
  runId: string
  manifestId: string
  generatedAt: string
  mode: string
  collection: string
  counts: {
    sources: number
    sourcesWithProjectedKnowledge: number
    evidence: number
    concepts: number
    relationships: number
    renderableRelationships: number
    fragments: number
    humanVerified: number
  }
  sourceCategories: Record<string, number>
  domains: Record<string, number>
  origins: Record<string, number>
  knowledgeTypes: Record<string, number>
  governance: {
    pendingHumanReviewSources: number
    contradictionChecksPending: number
    quarantinedMethods: number
    productionCollectionTouched: boolean
  }
}

export function observationFromStats(stats: CorpusStatsResponse): CorpusObservation {
  return {
    runId: stats.run_id,
    manifestId: stats.manifest_id,
    generatedAt: stats.generated_at,
    mode: stats.mode,
    collection: stats.collection,
    counts: {
      sources: stats.sources,
      sourcesWithProjectedKnowledge: stats.sources_with_projected_knowledge,
      evidence: stats.evidence_cards,
      concepts: stats.knowledge_nodes,
      relationships: stats.relationships,
      renderableRelationships: stats.renderable_relationships,
      fragments: stats.qdrant_points,
      humanVerified: stats.human_verified,
    },
    sourceCategories: stats.source_categories,
    domains: stats.distributions.domain_memberships,
    origins: stats.distributions.knowledge_origins,
    knowledgeTypes: stats.distributions.knowledge_types,
    governance: {
      pendingHumanReviewSources:
        stats.governance.pending_human_review_sources,
      contradictionChecksPending:
        stats.governance.contradiction_checks_pending,
      quarantinedMethods:
        stats.governance.procedural_method_projections_quarantined,
      productionCollectionTouched:
        stats.governance.production_collection_touched,
    },
  }
}
