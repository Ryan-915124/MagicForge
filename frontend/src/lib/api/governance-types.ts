import type {
  ClaimPolarity,
  ClaimRole,
  ContradictionStatus,
  EntityType,
  EvidenceClass,
  KnowledgeOrigin,
  MagicDomain,
  RelationType,
  SourceType,
} from "@/lib/api/types"

export type WorkflowStatus =
  | "submitted"
  | "approved"
  | "rejected"
  | "superseded"
  | "revoked"
export type ReviewDecision = "approve" | "reject"
export type SensitiveInformationLevel =
  | "public"
  | "controlled"
  | "secret_method"
  | "restricted"
export type ClaimEligibility =
  | "not_assessed"
  | "eligible"
  | "eligible_with_limits"
  | "ineligible"
export type ExtractionPermission =
  | "none"
  | "metadata_only"
  | "selected_sections"
  | "full_text"
export type StoragePermission =
  | "none"
  | "derived_knowledge_only"
  | "derived_with_short_excerpt"
export type ContradictionCheckStatus =
  | "not_checked"
  | "checked_none_found"
  | "checked_conflicts_linked"
  | "not_applicable"
export type SecretExposureLevel =
  | "none"
  | "general_principle"
  | "method_detail"
  | "operational_secret"
export type SourceCategory = "academic" | "practitioner" | "web"
export type ContentAccess =
  | "search_snippet"
  | "abstract"
  | "web_extract"
  | "full_text"
  | "pdf_text"

export interface PaginatedResponse<T> {
  items: T[]
  limit: number
  offset: number
}

export interface SearchProvenance {
  provider: "exa" | "tavily"
  tool_name: string
  query: string
  retrieved_at: string
  provider_result_id: string | null
  rank: number | null
}

export interface CitationMetadata {
  title: string
  authors: string[]
  year: number | null
  venue: string
  volume: string
  issue: string
  pages: string
  doi: string | null
  url: string
  peer_review_status: string
  provenance: SearchProvenance[]
}

export interface SourceAccessMetadata {
  access_method: string
  license_name: string | null
  rights_uri: string | null
  permission_notes: string
  redistribution_allowed: boolean
}

export interface CitationVerificationEvidence {
  id: string
  scope: "metadata" | "full_text"
  method: string
  resolver_result: "matched" | "mismatch" | "not_found" | "error"
  resolver_name: string
  verified_identifier: string
  checked_locator: string
  review_actor: string
  review_timestamp: string
  evidence_checksum: string
  actor_role_snapshot: string[]
  trusted_for_status: boolean
  notes: string
}

export interface SourceVersionSummary {
  id: string
  version: number
  content_hash: string
  content_access: ContentAccess
  source_type: SourceType
  knowledge_origin: KnowledgeOrigin
  sensitivity: SensitiveInformationLevel
  citation_status: string
  status: WorkflowStatus
  latest_permission_request_id: string | null
  created_at: string
}

export interface SourceReviewQueueItem {
  source_id: string
  source_version_id: string
  version: number
  canonical_key: string
  title: string
  source_category: SourceCategory
  source_type: SourceType
  knowledge_origin: KnowledgeOrigin
  content_access: ContentAccess
  citation_status: string
  status: WorkflowStatus
  sensitivity: SensitiveInformationLevel
  latest_permission_request_id: string | null
  created_at: string
}

export interface SourcePermissionRequestView {
  id: string
  source_version_id: string
  sequence: number
  requested_extraction_permission: ExtractionPermission
  requested_storage_permission: StoragePermission
  requested_scope_locators: string[]
  rights_basis: string
  rights_evidence: string[]
  reason: string
  submitted_by: string
  actor_role_snapshot: string[]
  request_checksum: string
  supersedes_request_id: string | null
  created_at: string
}

export interface SourceVersionReviewView extends SourceVersionSummary {
  citation: CitationMetadata
  access: SourceAccessMetadata
  requested_extraction_permission: ExtractionPermission
  requested_storage_permission: StoragePermission
  requested_scope_locators: string[]
  content: string
  citation_verification: CitationVerificationEvidence[]
  latest_permission_request: SourcePermissionRequestView | null
  permission_requests: SourcePermissionRequestView[]
}

export interface SourceReviewDetail {
  id: string
  canonical_key: string
  title: string
  source_category: SourceCategory
  versions: SourceVersionReviewView[]
}

export interface CitationVerificationInput {
  scope: "metadata" | "full_text"
  method:
    | "doi_resolver"
    | "canonical_locator"
    | "manual_metadata"
    | "content_checksum"
  resolver_result: "matched" | "mismatch" | "not_found" | "error"
  verified_identifier: string
  checked_locator: string
  resolver_name: string
  notes: string
}

export interface SourceReviewCommand {
  source_version_id: string
  source_permission_request_id: string
  decision: ReviewDecision
  reason: string
  claim_eligibility: ClaimEligibility
  extraction_permission: ExtractionPermission
  extraction_scope_locators: string[]
  storage_permission: StoragePermission
  sensitive_information_level: SensitiveInformationLevel
  contradicting_evidence_checked: ContradictionCheckStatus
  citation_verification: CitationVerificationInput[]
}

export interface SourceSummary {
  id: string
  canonical_key: string
  title: string
  source_category: SourceCategory
  versions: SourceVersionSummary[]
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

export interface ExtractionProvenance {
  producer: "human" | "glm" | "pipeline"
  extractor: string
  extraction_schema_version: string
  run_id: string | null
  llm_provider: "GLM" | null
  model: string | null
  tool_version: string | null
}

export interface ClaimCandidateSummary {
  id: string
  source_version_id: string
  claim: string
  claim_role: ClaimRole
  proposed_evidence_class: EvidenceClass
  status: WorkflowStatus
  candidate_checksum: string
  sensitivity: SensitiveInformationLevel
  submitted_at: string
}

export interface ClaimSourceContext {
  source_id: string
  source_version_id: string
  version: number
  title: string
  source_category: SourceCategory
  citation: CitationMetadata
  access: SourceAccessMetadata
  content: string
  content_access: ContentAccess
  source_type: SourceType
  knowledge_origin: KnowledgeOrigin
  sensitivity: SensitiveInformationLevel
  citation_status: string
}

export interface ClaimCandidateReviewView extends ClaimCandidateSummary {
  claim_polarity: ClaimPolarity
  applicable_domain: MagicDomain[]
  ontology_paths: string[]
  topic_tags: string[]
  mechanism_ids: string[]
  mechanism_status: "linked" | "unresolved" | "not_applicable"
  principle_ids: string[]
  magic_application: string | null
  application_origin: "source_stated" | "reviewer_synthesis" | "not_applicable"
  locator: EvidenceLocator
  evidence_excerpt: string
  proposed_limitations: string[]
  population_context: string | null
  performance_context: string | null
  extraction_confidence: number
  extraction_provenance: ExtractionProvenance
  candidate_schema_version: string
  source_context: ClaimSourceContext
}

export interface ConfidenceDimensionInput {
  score: 0 | 0.5 | 1
  reason: string
}

export interface ConfidenceAssessmentInput {
  provenance_quality: ConfidenceDimensionInput
  method_rigor: ConfidenceDimensionInput
  claim_directness: ConfidenceDimensionInput
  consistency: ConfidenceDimensionInput
  magic_applicability: ConfidenceDimensionInput
  assessed_by: string
}

export interface ClaimReviewCommand {
  decision: ReviewDecision
  reason: string
  confidence: ConfidenceAssessmentInput | null
  claim_eligibility: ClaimEligibility
  storage_permission: StoragePermission
  sensitive_information_level: SensitiveInformationLevel
  contradiction_status: ContradictionStatus
  contradicting_evidence_checked: ContradictionCheckStatus
  contradicting_evidence_ids: string[]
  limitations: string[]
  secret_exposure_level: SecretExposureLevel
}

export interface ClaimReviewResult {
  claim: ClaimCandidateSummary
  evidence_card_id: string | null
  evidence_version_id: string | null
}

export interface EvidenceVersionView {
  evidence_card_id: string
  evidence_version_id: string
  version: number
  schema_version: string
  claim: string
  claim_role: ClaimRole
  evidence_class: EvidenceClass
  knowledge_origin: KnowledgeOrigin
  applicable_domain: MagicDomain[]
  ontology_paths: string[]
  source_type: SourceType
  source_year: number | null
  citation_id: string
  source_locator: string | null
  evidence_excerpt: string | null
  limitations: string[]
  confidence_score: number
  confidence_label: string
  contradiction_status: ContradictionStatus
  sensitivity: SensitiveInformationLevel
  secret_exposure_level: SecretExposureLevel
  created_at: string
}

export type MappingProposalKind = "entity" | "relationship"
export type CanonicalResolution = "create" | "reuse" | "merge"

export interface MappingProposalSummary {
  id: string
  kind: MappingProposalKind
  status: WorkflowStatus
  schema_version: string
  proposal_checksum: string
  subject: string
  proposer_user_id: string | null
  proposer_run_id: string | null
  sensitivity: SensitiveInformationLevel
  submitted_at: string
  approved_artifact_id: string | null
  approved_artifact_version: number | null
}

interface MappingProposalBase {
  schema_version: "mapping-proposal-0.1"
  domains: MagicDomain[]
  ontology_paths: string[]
  topic_tags: string[]
  knowledge_origin: KnowledgeOrigin
  evidence_excerpt: string
  source_locator: string
  extraction_confidence: number
  supporting_evidence_version_ids: string[]
  limitations: string[]
  proposer_run_id: string | null
  supersedes_proposal_id: string | null
}

export interface EntityMappingProposal extends MappingProposalBase {
  entity: {
    id: string
    type: EntityType
    name: string
    description: string | null
    aliases: string[]
    attributes: Record<string, unknown>
  }
  definition: string
}

export interface RelationshipMappingProposal extends MappingProposalBase {
  source_entity_id: string
  target_entity_id: string
  relation_type: RelationType
  assertion: string
}

export interface ValidationRunView {
  id: string
  phase: string
  rule_version: string
  passed: boolean
  results: Record<string, unknown>
  created_at: string
}

export interface MappingProposalDetail extends MappingProposalSummary {
  proposal: EntityMappingProposal | RelationshipMappingProposal
  supporting_evidence_version_ids: string[]
  validation_runs: ValidationRunView[]
}

export interface MappingReviewCommand {
  decision: ReviewDecision
  reason: string
  confidence: ConfidenceAssessmentInput | null
  canonical_resolution: CanonicalResolution | null
  canonical_entity_id: string | null
}

export interface MappingReviewResult {
  mapping: MappingProposalSummary
  knowledge_node_version_id: string | null
  relationship_assertion_id: string | null
}

export interface CanonicalEntitySummary {
  id: string
  entity_type: EntityType
  canonical_name: string
  canonical_key: string
  aliases: string[]
  description: string
  status: "active"
  latest_knowledge_node_version_id: string
  latest_version: number
}

export type ArtifactType = "evidence_card" | "knowledge_node" | "relationship"

export interface EligibleArtifactView {
  artifact_type: ArtifactType
  artifact_row_id: string
  artifact_domain_id: string
  artifact_version: number
  payload_checksum: string
  sensitivity: SensitiveInformationLevel
  subject: string
  supporting_evidence_version_ids: string[]
  approved_at: string
}

export type ManifestStatus = "pending" | "authorized" | "ingested"

export interface ManifestBuildCommand {
  corpus_id: string
  collection_name: string
  evidence_version_ids: string[]
  knowledge_node_version_ids: string[]
  relationship_assertion_ids: string[]
  authorized_sensitive_levels: SensitiveInformationLevel[]
}

export interface ManifestItemView {
  artifact_type: ArtifactType
  artifact_row_id: string
  artifact_domain_id: string
  artifact_version: number
  payload_checksum: string
  projection_checksum: string
  projection_point_id: string
  sensitivity: SensitiveInformationLevel
}

export interface StorageManifestSummaryView {
  id: string
  corpus_id: string
  manifest_hash: string
  collection_name: string
  schema_version: string
  projection_schema_version: string
  status: ManifestStatus
  expected_point_count: number
  created_by_user_id: string
  created_at: string
  authorized_by_user_id: string | null
  authorized_at: string | null
}

export interface StorageManifestView extends StorageManifestSummaryView {
  validation_rule_versions: Record<string, string>
  authorized_sensitive_levels: SensitiveInformationLevel[]
  items: ManifestItemView[]
}

export interface CorpusVersionView {
  corpus_id: string
  manifest_id: string
  ingestion_receipt_id: string
  runtime_scope: string
  qdrant_collection: string
  schema_version: string
  projection_version: string
  vector_size: number
  vector_distance: string
  activation_state: "staged" | "active" | "inactive"
  created_at: string
  activated_at: string | null
}

export interface CorpusVersionsView {
  items: CorpusVersionView[]
}
