import { magicForgeRequest } from "@/lib/api/client"
import type { EntityType } from "@/lib/api/types"
import type {
  ArtifactType,
  CanonicalEntitySummary,
  ClaimCandidateReviewView,
  ClaimCandidateSummary,
  ClaimReviewCommand,
  ClaimReviewResult,
  CorpusVersionView,
  CorpusVersionsView,
  EligibleArtifactView,
  EvidenceVersionView,
  ManifestBuildCommand,
  MappingProposalDetail,
  MappingProposalKind,
  MappingProposalSummary,
  MappingReviewCommand,
  MappingReviewResult,
  PaginatedResponse,
  SourceReviewCommand,
  SourceReviewDetail,
  SourceReviewQueueItem,
  SourceSummary,
  StorageManifestSummaryView,
  StorageManifestView,
  WorkflowStatus,
} from "@/lib/api/governance-types"
import { readCsrfToken } from "@/lib/auth/csrf"

function queryString(values: Record<string, string | number | null | undefined>) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== "") {
      params.set(key, String(value))
    }
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ""
}

function mutation<T>(path: string, payload: unknown, idempotencyKey: string) {
  const csrfToken = readCsrfToken()
  if (!csrfToken) {
    return Promise.reject(new Error("The browser session is missing its CSRF token. Sign in again."))
  }
  return magicForgeRequest<T>(path, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Idempotency-Key": idempotencyKey,
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify(payload),
  })
}

export const governanceApi = {
  sourceQueue(status: WorkflowStatus | null = "submitted", offset = 0, limit = 50) {
    return magicForgeRequest<PaginatedResponse<SourceReviewQueueItem>>(
      `/api/magicforge/governance/review/sources${queryString({ status, limit, offset })}`,
      { cache: "no-store" }
    )
  },
  source(id: string) {
    return magicForgeRequest<SourceReviewDetail>(
      `/api/magicforge/governance/sources/${encodeURIComponent(id)}`,
      { cache: "no-store" }
    )
  },
  reviewSource(id: string, command: SourceReviewCommand, idempotencyKey: string) {
    return mutation<SourceSummary>(
      `/api/magicforge/governance/sources/${encodeURIComponent(id)}/review`,
      command,
      idempotencyKey
    )
  },
  claimQueue(status: WorkflowStatus | null = "submitted", offset = 0, limit = 50) {
    return magicForgeRequest<PaginatedResponse<ClaimCandidateSummary>>(
      `/api/magicforge/governance/review/claims${queryString({ status, limit, offset })}`,
      { cache: "no-store" }
    )
  },
  claim(id: string) {
    return magicForgeRequest<ClaimCandidateReviewView>(
      `/api/magicforge/governance/claims/${encodeURIComponent(id)}`,
      { cache: "no-store" }
    )
  },
  reviewClaim(id: string, command: ClaimReviewCommand, idempotencyKey: string) {
    return mutation<ClaimReviewResult>(
      `/api/magicforge/governance/claims/${encodeURIComponent(id)}/review`,
      command,
      idempotencyKey
    )
  },
  mappingQueue(status: WorkflowStatus | null = "submitted", kind?: MappingProposalKind, offset = 0, limit = 50) {
    return magicForgeRequest<PaginatedResponse<MappingProposalSummary>>(
      `/api/magicforge/governance/review/mappings${queryString({ status, kind, limit, offset })}`,
      { cache: "no-store" }
    )
  },
  mapping(id: string) {
    return magicForgeRequest<MappingProposalDetail>(
      `/api/magicforge/governance/mappings/${encodeURIComponent(id)}`,
      { cache: "no-store" }
    )
  },
  reviewMapping(id: string, command: MappingReviewCommand, idempotencyKey: string) {
    return mutation<MappingReviewResult>(
      `/api/magicforge/governance/mappings/${encodeURIComponent(id)}/review`,
      command,
      idempotencyKey
    )
  },
  canonicalEntities(query: string, entityType?: EntityType) {
    return magicForgeRequest<PaginatedResponse<CanonicalEntitySummary>>(
      `/api/magicforge/governance/knowledge/entities${queryString({ query, entity_type: entityType, limit: 50 })}`,
      { cache: "no-store" }
    )
  },
  evidenceVersion(id: string) {
    return magicForgeRequest<{ items: EvidenceVersionView[] }>(
      `/api/magicforge/governance/evidence/${encodeURIComponent(id)}/versions`,
      { cache: "no-store" }
    )
  },
  eligibleArtifacts(artifactType?: ArtifactType, offset = 0, limit = 50) {
    return magicForgeRequest<PaginatedResponse<EligibleArtifactView>>(
      `/api/magicforge/governance/storage/eligible-artifacts${queryString({ artifact_type: artifactType, limit, offset })}`,
      { cache: "no-store" }
    )
  },
  manifests(status?: "pending" | "authorized" | "ingested", offset = 0, limit = 50) {
    return magicForgeRequest<PaginatedResponse<StorageManifestSummaryView>>(
      `/api/magicforge/governance/storage/manifests${queryString({ status, limit, offset })}`,
      { cache: "no-store" }
    )
  },
  manifest(id: string) {
    return magicForgeRequest<StorageManifestView>(
      `/api/magicforge/governance/storage/manifests/${encodeURIComponent(id)}`,
      { cache: "no-store" }
    )
  },
  buildManifest(command: ManifestBuildCommand, idempotencyKey: string) {
    return mutation<StorageManifestView>(
      "/api/magicforge/governance/storage/manifests",
      command,
      idempotencyKey
    )
  },
  authorizeManifest(id: string, reason: string, idempotencyKey: string) {
    return mutation<StorageManifestView>(
      `/api/magicforge/governance/storage/manifests/${encodeURIComponent(id)}/authorize`,
      { reason },
      idempotencyKey
    )
  },
  corpora() {
    return magicForgeRequest<CorpusVersionsView>("/api/magicforge/governance/corpora", {
      cache: "no-store",
    })
  },
  activeCorpus(runtimeScope = "production") {
    return magicForgeRequest<CorpusVersionView>(
      `/api/magicforge/governance/corpora/active${queryString({ runtime_scope: runtimeScope })}`,
      { cache: "no-store" }
    )
  },
  activateCorpus(id: string, reason: string, idempotencyKey: string) {
    return mutation<CorpusVersionView>(
      `/api/magicforge/governance/corpora/${encodeURIComponent(id)}/activate`,
      { runtime_scope: "production", reason },
      idempotencyKey
    )
  },
}
