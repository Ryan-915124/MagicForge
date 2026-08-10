import type {
  CitationVerificationInput,
  ExtractionPermission,
  SourcePermissionRequestView,
  StoragePermission,
} from "@/lib/api/governance-types"

const EXTRACTION_RANK: Record<ExtractionPermission, number> = {
  none: 0,
  metadata_only: 1,
  selected_sections: 2,
  full_text: 3,
}

const STORAGE_RANK: Record<StoragePermission, number> = {
  none: 0,
  derived_knowledge_only: 1,
  derived_with_short_excerpt: 2,
}

export function extractionWithinRequestCeiling(
  value: ExtractionPermission,
  ceiling: ExtractionPermission
) {
  return EXTRACTION_RANK[value] <= EXTRACTION_RANK[ceiling]
}

export function storageWithinRequestCeiling(
  value: StoragePermission,
  ceiling: StoragePermission
) {
  return STORAGE_RANK[value] <= STORAGE_RANK[ceiling]
}

export function requestAllowsApproval(request: SourcePermissionRequestView) {
  return request.requested_extraction_permission === "selected_sections"
    || request.requested_extraction_permission === "full_text"
}

export function extractionSupportsClaimApproval(value: ExtractionPermission) {
  return value === "selected_sections" || value === "full_text"
}

export function citationVerificationReadyForApproval(
  verification: CitationVerificationInput
) {
  return verification.resolver_result === "matched"
    && verification.verified_identifier.trim().length > 0
    && verification.checked_locator.trim().length > 0
    && verification.resolver_name.trim().length > 0
}

export function scopeWithinRequestCeiling(
  approvedScope: string[],
  approvedPermission: ExtractionPermission,
  request: SourcePermissionRequestView
) {
  if (approvedPermission !== "selected_sections") return true
  if (approvedScope.length === 0) return false
  if (request.requested_extraction_permission !== "selected_sections") return true

  const normalize = (value: string) => value.trim().toLocaleLowerCase("en-US")
  const requested = new Set(request.requested_scope_locators.map(normalize))
  return approvedScope.every((locator) => requested.has(normalize(locator)))
}

export function permissionRequestRespectsAccess(
  request: SourcePermissionRequestView,
  redistributionAllowed: boolean
) {
  return !(
    request.requested_storage_permission === "derived_with_short_excerpt" &&
    !redistributionAllowed
  )
}
