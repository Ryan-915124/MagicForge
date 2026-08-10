export type KnowledgeOrigin =
  | "scientific_evidence"
  | "expert_practice"
  | "personal_interpretation"

export type EvidenceLevel = "empirical" | "review" | "practitioner" | "anecdotal"

export type EvidenceClass =
  | "controlled_experiment"
  | "quasi_experiment"
  | "observational_study"
  | "systematic_review"
  | "meta_analysis"
  | "narrative_review"
  | "expert_instruction"
  | "expert_case_analysis"
  | "practitioner_report"
  | "historical_primary_record"
  | "historical_secondary_analysis"
  | "analyst_interpretation"
  | "anecdotal_observation"

export type ClaimRole =
  | "result"
  | "method"
  | "background"
  | "hypothesis"
  | "discussion"
  | "expert_opinion"
  | "context_only"

export type ClaimPolarity = "supports" | "contradicts" | "qualifies"
export type MagicDomain = "card" | "close-up" | "stage" | "mentalism" | "theory"
export type ConfidenceLabel = "insufficient" | "low" | "moderate" | "high"
export type ContradictionStatus = "not_checked" | "none_found" | "resolved" | "unresolved"

export type SourceType =
  | "journal_article"
  | "conference_paper"
  | "preprint"
  | "academic_book"
  | "book_chapter"
  | "practitioner_book"
  | "web_article"
  | "interview"
  | "transcript"
  | "archival_material"
  | "internal_analysis"

export type EntityType =
  | "effect"
  | "technique"
  | "method"
  | "psychology_principle"
  | "performer"
  | "source"
  | "cognitive_mechanism"
  | "research_paper"

export type RelationType =
  | "uses"
  | "inspired_by"
  | "requires"
  | "explains"
  | "performed_by"
  | "related_to"

export type KnowledgeType =
  | "effect"
  | "method"
  | "technique"
  | "psychology"
  | "evidence"
  | "performance"

export interface SourceSummary {
  title: string
  author: string
  score: number
  document_id: string | null
  entity_ids: string[]
  source_locator: string | null
  page_number: number | null
  magic_category: string
  artifact_type: string
  knowledge_type: string
  knowledge_origin: KnowledgeOrigin | string
  evidence_level: EvidenceLevel | string
  evidence_class: EvidenceClass | string
  confidence: number | null
  confidence_label: ConfidenceLabel | string
  limitations: string[]
  contradiction_status: ContradictionStatus | string
  evidence_card_id: string | null
}

export interface ChatRequest {
  question: string
}

export type MagicChatActKind =
  | "effect"
  | "hidden_structure"
  | "cognitive_mechanism"

export interface MagicChatAnswerAct {
  kind: MagicChatActKind
  content: string
}

export interface GenerationResponse {
  result: string
  sources: SourceSummary[]
  answer_format_version?: "magicforge.reveal.v1" | string | null
  lead?: string | null
  acts?: MagicChatAnswerAct[]
  synthesis?: string | null
}

export interface HealthResponse {
  status: "ok"
  glm_configured: boolean
  qdrant_url: string
  collection: string
  mode: "bootstrap" | "production" | string
  profile?: "demo" | "development" | "production" | string
  read_only?: boolean
  synthetic_corpus?: boolean
  corpus_id?: string
  schema_version?: string
  manifest_schema_version?: string
  manifest_id?: string
}

export interface ConfidenceDimension {
  score: 0 | 0.5 | 1
  reason: string
}

export interface ConfidenceAssessment {
  provenance_quality: ConfidenceDimension
  method_rigor: ConfidenceDimension
  claim_directness: ConfidenceDimension
  consistency: ConfidenceDimension
  magic_applicability: ConfidenceDimension
  score: number
  label: ConfidenceLabel
  assessed_by: string
  assessed_at: string
}

export interface EvidenceSource {
  citation_id: string
  source_id: string
  source_candidate_id: string
  source_version_id: string
  document_id: string
  source_type: SourceType
  research_paper_id: string | null
  citation_status: string
  peer_review_status: string
  source_year: number | null
}

export interface EvidenceLocator {
  media_type: string
  source_locator: string
  page_number: number | null
  printed_page: string | null
  section: string | null
  paragraph: number | null
  figure_or_table: string | null
  timestamp_start: number | null
  timestamp_end: number | null
}

export interface EvidenceReview {
  review_item_id: string | null
  claim_eligibility: "not_assessed" | "eligible" | "eligible_with_limits" | "ineligible"
  extraction_permission: "none" | "metadata_only" | "selected_sections" | "full_text"
  storage_permission: "none" | "derived_knowledge_only" | "derived_with_short_excerpt"
  approved: boolean
  review_status:
    | "pending"
    | "approved"
    | "rejected"
    | "ingested"
    | "bootstrap_pending_human_review"
    | "bootstrap_generated"
    | "bootstrap"
  reviewer: string | null
  review_date: string | null
  approval_reason: string | null
  contradicting_evidence_checked:
    | "not_checked"
    | "checked_none_found"
    | "checked_conflicts_linked"
    | "not_applicable"
  sensitive_information_level: "public" | "controlled" | "secret_method" | "restricted"
}

export interface EvidenceCard {
  id: string
  schema_version: string
  version: number
  canonical_claim_id: string
  claim: string
  claim_role: ClaimRole
  claim_polarity: ClaimPolarity
  mechanism_ids: string[]
  mechanism_status: "linked" | "unresolved" | "not_applicable"
  principle_ids: string[]
  applicable_domain: MagicDomain[]
  ontology_paths: string[]
  topic_tags: string[]
  magic_application: string | null
  application_origin: "source_stated" | "reviewer_synthesis" | "not_applicable"
  knowledge_origin: KnowledgeOrigin
  evidence_class: EvidenceClass
  evidence_level: EvidenceLevel
  source: EvidenceSource
  locator: EvidenceLocator
  evidence_excerpt: string
  excerpt_hash: string
  limitations: string[]
  population_context: string | null
  performance_context: string | null
  confidence: ConfidenceAssessment | null
  extraction_confidence: number
  contradiction_status: ContradictionStatus
  contradicting_evidence_ids: string[]
  supersedes: string[]
  review: EvidenceReview
  secret_exposure_level: "none" | "general_principle" | "method_detail" | "operational_secret"
  created_at: string
  created_by: string
}

export interface KnowledgeEntity {
  id: string
  type: EntityType
  name: string
  description: string | null
  aliases: string[]
  attributes: Record<string, unknown>
}

export interface KnowledgeRelationship {
  id: string
  source_id: string
  target_id: string
  type: RelationType
  evidence: string | null
  confidence: number | null
  source_chunk_id: string | null
  attributes: Record<string, unknown>
}

export interface KnowledgeNodeVersion {
  id: string
  schema_version?: string
  entity: KnowledgeEntity
  version?: number
  definition: string
  domains: MagicDomain[]
  ontology_paths: string[]
  topic_tags: string[]
  knowledge_origin: KnowledgeOrigin
  supporting_evidence_ids: string[]
  contradicting_evidence_ids: string[]
  limitations: string[]
  confidence: ConfidenceAssessment
  approved?: boolean
  verification_status?: "unverified" | "verified"
  bootstrap_generated?: boolean
  human_verified?: boolean
  created_by?: string
  created_at?: string
}

export interface SearchResult {
  text: string
  score: number
  payload: Record<string, unknown>
}

export interface KnowledgeSearchFilters {
  query: string
  limit?: number
  knowledge_types?: string[]
  domains?: string[]
  ontology_paths?: string[]
  knowledge_origins?: string[]
  evidence_levels?: string[]
  entity_ids?: string[]
  entity_types?: string[]
  relation_types?: string[]
}

export interface KnowledgeSearchResponse {
  results: SearchResult[]
  evidence_cards: EvidenceCard[]
  nodes: KnowledgeNodeVersion[]
  relationships: KnowledgeRelationship[]
  projection: GraphProjectionSummary
}

export interface GraphProjectionSummary {
  run_id: string
  collection: string
  manifest_id: string
  generated_at: string
  sources: number
  knowledge_nodes: number
  relationships: number
  renderable_relationships: number
  evidence_cards: number
  qdrant_points: number
  bootstrap_generated: boolean
  human_verified: boolean
}

export interface CorpusStatsResponse {
  run_id: string
  manifest_id: string
  generated_at: string
  mode: string
  collection: string
  sources: number
  sources_with_projected_knowledge: number
  source_categories: Record<string, number>
  evidence_cards: number
  knowledge_nodes: number
  relationships: number
  renderable_relationships: number
  qdrant_points: number
  human_verified: number
  distributions: {
    scope: "projected_points" | string
    artifact_types: Record<string, number>
    domain_memberships: Record<string, number>
    knowledge_origins: Record<string, number>
    knowledge_types: Record<string, number>
  }
  governance: {
    pending_human_review_sources: number
    contradiction_checks_pending: number
    procedural_method_projections_quarantined: number
    production_collection_touched: boolean
  }
}

export interface ResearchPipelineStage {
  id: string
  label: string
  status: "completed" | "receipt_verified"
  metrics: Record<string, number>
}

export interface ResearchRunHistoryItem {
  run_id: string
  generated_at: string
  mode: "bootstrap"
  collection: string
  sources: number
  extracted_sources: number
  claims: number
  evidence_cards: number
  knowledge_nodes: number
  relationships: number
  qdrant_points: number
  extraction_errors: number
  status: "reported_succeeded" | "receipt_verified"
  metric_basis: "reported_generated_outputs" | "receipt_verified_projections"
}

export interface ResearchConsoleResponse {
  observed_at: string
  current_run: {
    run_id: string
    mode: "bootstrap"
    generated_at: string
    collection: string
    status: "receipt_verified"
  }
  runtime: {
    api: {
      status: "ok"
    }
    intelligence_instrument: {
      provider: "Zhipu GLM"
      model: string
      configured: boolean
      connectivity: "not_probed"
      structured_extraction: boolean
    }
    retrieval: {
      configured: boolean
      collection: string
      storage_kind: "local" | "remote"
      connectivity: "not_probed"
    }
  }
  pipeline: {
    status: "receipt_verified"
    stages: ResearchPipelineStage[]
  }
  memory_vault: {
    runtime_collection: string
    audited_collection: string
    alignment_status: "aligned" | "configuration_mismatch"
    manifest: {
      status: "manifest_verified"
      id: string
      hash: string
      point_count: number
    }
    receipt: {
      status: "receipt_verified"
      id: string
      ingested_at: string
      point_count: number
    }
    retrieval_smoke: {
      status: "report_verified"
      tested_at: string
      query_count: number
      collection_count: number
      all_returned_hits_bootstrap_safe: boolean
    }
    points: {
      manifest: number
      receipt: number
      smoke_observed: number
    }
    safety: {
      bootstrap_generated_points: number
      human_verified_points: number
      approved_points: number
      storage_permission_points: number
      production_collection_touched: boolean
      production_collection_present_in_smoke: boolean
      safety_excluded_projection_count: number
    }
  }
  governance: {
    mode: "bootstrap"
    checkpoint_status: "pending_human_review"
    sources_pending: number
    evidence_cards_pending: number
    knowledge_nodes_pending: number
    relationships_pending: number
    contradiction_checks_pending: number
    procedural_method_projections_quarantined: number
    human_verified_points: number
    approved_points: number
    storage_permission_points: number
  }
  run_history: ResearchRunHistoryItem[]
}

export type MagicForgeRole = "reader" | "reviewer" | "operator" | "admin"
export type SessionTransport = "cookie" | "bearer"

export interface AuthenticatedActor {
  id: string
  username: string
  roles: MagicForgeRole[]
  session_id: string
  session_transport: SessionTransport
  session_expires_at: string
}

export interface LoginResponse {
  actor: AuthenticatedActor
  transport: SessionTransport
  token_type: "Bearer" | null
  access_token: string | null
  csrf_required: boolean
}

export interface LogoutResponse {
  revoked: boolean
}

export type ApiErrorCode =
  | "endpoint_unavailable"
  | "alpha_feature_unavailable"
  | "backend_unreachable"
  | "upstream_timeout"
  | "backend_error"
  | "invalid_request"
  | "unauthenticated"
  | "invalid_credentials"
  | "invalid_authorization_header"
  | "multiple_credentials"
  | "origin_validation_failed"
  | "csrf_validation_failed"
  | "csrf_cookie_missing"
  | "authentication_not_ready"
  | "authentication_dependency_unavailable"
  | "forbidden"
  | (string & {})

export interface ApiErrorPayload {
  error: {
    code: ApiErrorCode
    message: string
    upstream_path?: string
  }
}

export interface FastApiErrorPayload {
  detail:
    | string
    | {
        code?: string
        message?: string
      }
}
