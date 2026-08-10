"use client"

import { useEffect, useMemo, useState, type FormEvent } from "react"
import { CheckCircle2Icon, SearchIcon, ShieldXIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import type { AuthenticatedActor } from "@/lib/api/types"
import { governanceApi } from "@/lib/api/governance-client"
import type {
  CanonicalEntitySummary,
  CanonicalResolution,
  CitationVerificationInput,
  ClaimCandidateReviewView,
  ClaimEligibility,
  ClaimReviewCommand,
  ConfidenceAssessmentInput,
  ContradictionCheckStatus,
  EvidenceVersionView,
  ExtractionPermission,
  MappingProposalDetail,
  MappingReviewCommand,
  ReviewDecision,
  SecretExposureLevel,
  SensitiveInformationLevel,
  SourcePermissionRequestView,
  SourceReviewCommand,
  SourceReviewDetail,
  StoragePermission,
} from "@/lib/api/governance-types"

import {
  ConfidenceEditor,
  DetailValue,
  LabeledSelect,
  MutationConfirmation,
  StatusBadge,
  blankConfidence,
  errorText,
  lines,
  shortId,
} from "./governance-common"
import { useStableMutationKey } from "./use-stable-mutation-key"
import { useGuardedOpenChange } from "./use-unsaved-warning"
import {
  citationVerificationReadyForApproval,
  extractionSupportsClaimApproval,
  extractionWithinRequestCeiling,
  permissionRequestRespectsAccess,
  requestAllowsApproval,
  scopeWithinRequestCeiling,
  storageWithinRequestCeiling,
} from "./source-permission-policy"
import styles from "./governance.module.css"

interface SheetLifecycle {
  open: boolean
  onOpenChange: (open: boolean) => void
  onReviewed: () => void
}

function LoadingDetail() {
  const { t } = useLocale()
  return (
    <div className="space-y-3 p-5" role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{t("governance.review.loadingDetail")}</span>
      <div className="space-y-3" aria-hidden="true">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    </div>
  )
}

const decisionOptions = (approve: string, reject: string) => [
  { value: "approve" as const, label: approve },
  { value: "reject" as const, label: reject },
]

function safeHttpUrl(value: string) {
  try {
    const url = new URL(value)
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null
  } catch {
    return null
  }
}

function draftSignature(value: unknown) {
  return JSON.stringify(value)
}

function confidenceConfirmationDetails(confidence: ConfidenceAssessmentInput | null, t: Translator) {
  if (!confidence) return []
  return [
    { label: t("governance.confidence.provenance"), value: `${confidence.provenance_quality.score} · ${confidence.provenance_quality.reason}` },
    { label: t("governance.confidence.rigor"), value: `${confidence.method_rigor.score} · ${confidence.method_rigor.reason}` },
    { label: t("governance.confidence.directness"), value: `${confidence.claim_directness.score} · ${confidence.claim_directness.reason}` },
    { label: t("governance.confidence.consistency"), value: `${confidence.consistency.score} · ${confidence.consistency.reason}` },
    { label: t("governance.confidence.applicability"), value: `${confidence.magic_applicability.score} · ${confidence.magic_applicability.reason}` },
  ]
}

function PermissionRequestRecord({
  request,
  latest,
  formatDateTime,
}: {
  request: SourcePermissionRequestView
  latest?: boolean
  formatDateTime: (value: string) => string
}) {
  const { t } = useLocale()
  return (
    <article className={styles.permissionRequest} data-latest={latest ? "true" : "false"}>
      <div className={styles.permissionRequestHeader}>
        <div>
          <p className={styles.microLabel}>{t("governance.field.permissionRequestId")}</p>
          <p className={`${styles.mono} ${styles.permissionRequestId}`} translate="no">{request.id}</p>
        </div>
        <Badge variant={latest ? "default" : "outline"}>
          {latest ? t("governance.source.latestRequest") : t("governance.source.priorRequest")} · #{request.sequence}
        </Badge>
      </div>
      <div className={styles.detailGrid}>
        <DetailValue label={t("governance.field.sourceVersionId")}><span className={styles.mono} translate="no">{request.source_version_id}</span></DetailValue>
        <DetailValue label={t("governance.field.permissionRequestExtraction")}>{request.requested_extraction_permission}</DetailValue>
        <DetailValue label={t("governance.field.permissionRequestStorage")}>{request.requested_storage_permission}</DetailValue>
        <DetailValue label={t("governance.field.rightsBasis")}>{request.rights_basis}</DetailValue>
        <DetailValue label={t("governance.field.submittedBy")}><span translate="no">{request.submitted_by}</span></DetailValue>
        <DetailValue label={t("governance.field.requestedScope")}>{request.requested_scope_locators.join(" · ") || "—"}</DetailValue>
        <DetailValue label={t("governance.field.createdAt")}>{formatDateTime(request.created_at)}</DetailValue>
        <DetailValue label={t("governance.field.requestChecksum")}><span className={styles.mono} translate="no">{request.request_checksum}</span></DetailValue>
        <DetailValue label={t("governance.field.supersedesRequest")}><span translate="no">{request.supersedes_request_id || "—"}</span></DetailValue>
        <DetailValue label={t("governance.field.actorRoles")}>{request.actor_role_snapshot.join(", ") || "—"}</DetailValue>
        <DetailValue label={t("governance.field.requestReason")}>{request.reason}</DetailValue>
      </div>
      <div className={styles.rightsEvidence}>
        <p className={styles.microLabel}>{t("governance.field.rightsEvidence")}</p>
        {request.rights_evidence.length > 0 ? (
          <ul>
            {request.rights_evidence.map((item, index) => <li key={`${request.id}:${index}`}>{item}</li>)}
          </ul>
        ) : <p className={styles.muted}>{t("governance.source.noRightsEvidence")}</p>}
      </div>
    </article>
  )
}

export function SourceReviewSheet({
  open,
  sourceId,
  sourceVersionId,
  onOpenChange,
  onReviewed,
}: SheetLifecycle & { sourceId: string; sourceVersionId: string }) {
  const { locale, t } = useLocale()
  const [detail, setDetail] = useState<SourceReviewDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<ReviewDecision | null>(null)
  const [reason, setReason] = useState("")
  const [eligibility, setEligibility] = useState<ClaimEligibility>("eligible_with_limits")
  const [extraction, setExtraction] = useState<ExtractionPermission>("selected_sections")
  const [scope, setScope] = useState("")
  const [storage, setStorage] = useState<StoragePermission>("derived_knowledge_only")
  const [sensitivity, setSensitivity] = useState<SensitiveInformationLevel>("controlled")
  const [contradiction, setContradiction] = useState<ContradictionCheckStatus>("not_checked")
  const [verification, setVerification] = useState<CitationVerificationInput>({
    scope: "metadata",
    method: "canonical_locator",
    resolver_result: "not_found",
    verified_identifier: "",
    checked_locator: "",
    resolver_name: "human-review",
    notes: "",
  })
  const [command, setCommand] = useState<SourceReviewCommand | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [baseline, setBaseline] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const { keyFor, clear } = useStableMutationKey(`source-review:${sourceVersionId}`)
  const dateTimeFormatter = useMemo(
    () => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }),
    [locale]
  )

  useEffect(() => {
    let active = true
    governanceApi.source(sourceId).then(
      (value) => {
        if (!active) return
        setDetail(value)
        const version = value.versions.find((item) => item.id === sourceVersionId)
        if (version) {
          const initialVerification: CitationVerificationInput = {
            scope: "metadata",
            method: version.citation.doi ? "doi_resolver" : "canonical_locator",
            resolver_result: "not_found",
            verified_identifier: version.citation.doi || version.citation.url,
            checked_locator: version.citation.url,
            resolver_name: "human-review",
            notes: "",
          }
          const permissionRequest = version.latest_permission_request
          const initialExtraction = permissionRequest?.requested_extraction_permission ?? "none"
          const initialStorage = permissionRequest?.requested_storage_permission ?? "none"
          const initialScope = permissionRequest?.requested_scope_locators.join("\n") ?? ""
          setExtraction(initialExtraction)
          setStorage(initialStorage)
          setSensitivity(version.sensitivity)
          setScope(initialScope)
          setVerification(initialVerification)
          setBaseline(draftSignature({
            decision: null,
            reason: "",
            eligibility: "eligible_with_limits",
            extraction: initialExtraction,
            scope: initialScope,
            storage: initialStorage,
            sensitivity: version.sensitivity,
            contradiction: "not_checked",
            verification: initialVerification,
            permissionRequestId: permissionRequest?.id ?? null,
          }))
        }
        setLoading(false)
      },
      (cause) => {
        if (!active) return
        setError(errorText(cause, t("governance.error.generic")))
        setLoading(false)
      }
    )
    return () => { active = false }
  }, [sourceId, sourceVersionId, t])

  const version = detail?.versions.find((item) => item.id === sourceVersionId) ?? null
  const permissionRequest = version?.latest_permission_request ?? null
  const requestIdentityMatches = Boolean(
    permissionRequest
      && version
      && version.latest_permission_request_id === permissionRequest.id
      && permissionRequest.source_version_id === version.id
  )
  const scopeLocators = lines(scope)
  const requestHasApprovalCeiling = permissionRequest ? requestAllowsApproval(permissionRequest) : false
  const requestIsNoneNone = permissionRequest?.requested_extraction_permission === "none"
    && permissionRequest.requested_storage_permission === "none"
  const requestMatchesAccess = permissionRequest && version
    ? permissionRequestRespectsAccess(permissionRequest, version.access.redistribution_allowed)
    : false
  const extractionWithinCeiling = permissionRequest
    ? extractionWithinRequestCeiling(extraction, permissionRequest.requested_extraction_permission)
    : false
  const storageWithinCeiling = permissionRequest
    ? storageWithinRequestCeiling(storage, permissionRequest.requested_storage_permission)
    : false
  const scopeWithinCeiling = permissionRequest
    ? scopeWithinRequestCeiling(scopeLocators, extraction, permissionRequest)
    : false
  const extractionSupportsApproval = extractionSupportsClaimApproval(extraction)
  const citationVerificationReady = citationVerificationReadyForApproval(verification)
  const approvalPreparationBlocked = !permissionRequest
    || !requestIdentityMatches
    || !requestHasApprovalCeiling
    || !requestMatchesAccess
    || !extractionSupportsApproval
    || !citationVerificationReady
    || !extractionWithinCeiling
    || !storageWithinCeiling
    || !scopeWithinCeiling
  const availableExtractionOptions = permissionRequest
    ? extractionOptions(t).filter((option) => extractionWithinRequestCeiling(option.value, permissionRequest.requested_extraction_permission))
    : extractionOptions(t).filter((option) => option.value === "none")
  const availableStorageOptions = permissionRequest
    ? storageOptions(t).filter((option) => (
      storageWithinRequestCeiling(option.value, permissionRequest.requested_storage_permission)
      && (option.value !== "derived_with_short_excerpt" || version?.access.redistribution_allowed === true)
    ))
    : storageOptions(t).filter((option) => option.value === "none")
  const earlierPermissionRequests = version?.permission_requests
    .filter((item) => item.id !== permissionRequest?.id)
    .toSorted((left, right) => right.sequence - left.sequence) ?? []
  const formatDateTime = (value: string) => {
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime()) ? value : dateTimeFormatter.format(parsed)
  }
  const currentDraft = draftSignature({ decision, reason, eligibility, extraction, scope, storage, sensitivity, contradiction, verification, permissionRequestId: permissionRequest?.id ?? null })
  const dirty = open && !submitted && baseline !== null && currentDraft !== baseline
  const closeGuard = useGuardedOpenChange({
    dirty,
    message: t("governance.unsaved.browserWarning"),
    onOpenChange,
  })

  const prepare = (event: FormEvent) => {
    event.preventDefault()
    if (!decision || !permissionRequest || !requestIdentityMatches) return
    if (decision === "approve" && approvalPreparationBlocked) return
    const next: SourceReviewCommand = decision === "reject" ? {
      source_version_id: sourceVersionId,
      source_permission_request_id: permissionRequest.id,
      decision,
      reason,
      claim_eligibility: "ineligible",
      extraction_permission: "none",
      extraction_scope_locators: [],
      storage_permission: "none",
      sensitive_information_level: sensitivity,
      contradicting_evidence_checked: "not_applicable",
      citation_verification: [],
    } : {
      source_version_id: sourceVersionId,
      source_permission_request_id: permissionRequest.id,
      decision,
      reason,
      claim_eligibility: eligibility,
      extraction_permission: extraction,
      extraction_scope_locators: extraction === "selected_sections" ? scopeLocators : [],
      storage_permission: storage,
      sensitive_information_level: sensitivity,
      contradicting_evidence_checked: contradiction,
      citation_verification: [verification],
    }
    setCommand(next)
  }

  const submit = async () => {
    if (!command) return
    setSubmitting(true)
    setError(null)
    try {
      await governanceApi.reviewSource(sourceId, command, keyFor(command))
      clear()
      setCommand(null)
      setSubmitted(true)
      onReviewed()
    } catch (cause) {
      setError(errorText(cause, t("governance.error.generic")))
      setCommand(null)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={closeGuard.guardedOnOpenChange}>
      <SheetContent className={styles.sheet}>
        <SheetHeader className={styles.sheetHeader}>
          <SheetTitle className={styles.sheetTitle}>{t("governance.source.title")}</SheetTitle>
          <SheetDescription>{t("governance.source.description")}</SheetDescription>
        </SheetHeader>
        {loading ? <LoadingDetail /> : error && !detail ? (
          <Alert variant="destructive" className={styles.error}><AlertTitle>{t("governance.error.detail")}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>
        ) : detail && version ? (
          <div className={styles.sheetScroll}>
            <section className={styles.sheetSection}>
              <h3>{t("governance.source.immutableVersion")}</h3>
              <div className={styles.detailGrid}>
                <DetailValue label={t("governance.field.sourceId")}>{detail.id}</DetailValue>
                <DetailValue label={t("governance.field.sourceVersionId")}>{version.id}</DetailValue>
                <DetailValue label={t("governance.term.source")}>{detail.title}</DetailValue>
                <DetailValue label={t("governance.field.version")}>v{version.version} · {shortId(version.id)}</DetailValue>
                <DetailValue label={t("governance.field.contentHash")}>{version.content_hash}</DetailValue>
                <DetailValue label={t("governance.field.citationStatus")}><StatusBadge status={version.citation_status} /></DetailValue>
                <DetailValue label={t("governance.field.contentAccess")}>{version.content_access}</DetailValue>
                <DetailValue label={t("governance.field.doi")}>{version.citation.doi || "—"}</DetailValue>
                <DetailValue label={t("governance.field.url")}>{safeHttpUrl(version.citation.url) ? <a href={safeHttpUrl(version.citation.url) ?? undefined} target="_blank" rel="noreferrer" className="underline underline-offset-4">{version.citation.url}</a> : version.citation.url}</DetailValue>
                <DetailValue label={t("governance.field.requestedExtraction")}>{version.requested_extraction_permission}</DetailValue>
                <DetailValue label={t("governance.field.requestedStorage")}>{version.requested_storage_permission}</DetailValue>
              </div>
              <div className={`${styles.sourceContent} mt-4`}>{version.content}</div>
            </section>

            <section className={styles.sheetSection}>
              <h3>{t("governance.source.accessMetadata")}</h3>
              <div className={styles.detailGrid}>
                <DetailValue label={t("governance.field.accessMethod")}>{version.access.access_method}</DetailValue>
                <DetailValue label={t("governance.field.license")}>{version.access.license_name || "—"}</DetailValue>
                <DetailValue label={t("governance.field.rightsUri")}>
                  {version.access.rights_uri && safeHttpUrl(version.access.rights_uri)
                    ? <a href={safeHttpUrl(version.access.rights_uri) ?? undefined} target="_blank" rel="noreferrer" className="underline underline-offset-4">{version.access.rights_uri}</a>
                    : version.access.rights_uri || "—"}
                </DetailValue>
                <DetailValue label={t("governance.field.redistributionAllowed")}>
                  <Badge variant={version.access.redistribution_allowed ? "secondary" : "outline"}>
                    {version.access.redistribution_allowed ? t("governance.value.yes") : t("governance.value.no")}
                  </Badge>
                </DetailValue>
                <div className={styles.full}>
                  <DetailValue label={t("governance.field.permissionNotes")}>{version.access.permission_notes || "—"}</DetailValue>
                </div>
              </div>
            </section>

            <section className={styles.sheetSection}>
              <h3>{t("governance.source.permissionRequest")}</h3>
              {permissionRequest ? (
                <>
                  <PermissionRequestRecord request={permissionRequest} latest formatDateTime={formatDateTime} />
                  {!requestIdentityMatches && (
                    <Alert variant="destructive" title={t("governance.source.requestIdentityMismatch")}>
                      {t("governance.source.requestIdentityMismatchDescription")}
                    </Alert>
                  )}
                  {!requestHasApprovalCeiling && (
                    <Alert className={styles.notice}>
                      <AlertTitle>{t("governance.source.permissionRequired")}</AlertTitle>
                      <AlertDescription>{t(requestIsNoneNone ? "governance.source.noneRequestDescription" : "governance.source.insufficientRequestDescription")}</AlertDescription>
                    </Alert>
                  )}
                  {!requestMatchesAccess && (
                    <Alert variant="destructive" className={styles.error}>
                      <AlertTitle>{t("governance.source.invalidPermissionRequest")}</AlertTitle>
                      <AlertDescription>{t("governance.source.redistributionConflict")}</AlertDescription>
                    </Alert>
                  )}
                  <details className={styles.permissionHistory}>
                    <summary>{t("governance.source.requestHistory", { count: version.permission_requests.length })}</summary>
                    {earlierPermissionRequests.length > 0 ? (
                      <div className={styles.permissionHistoryList}>
                        {earlierPermissionRequests.map((item) => (
                          <PermissionRequestRecord key={item.id} request={item} formatDateTime={formatDateTime} />
                        ))}
                      </div>
                    ) : <p className={styles.muted}>{t("governance.source.noPriorRequests")}</p>}
                  </details>
                </>
              ) : (
                <Alert className={styles.notice}>
                  <AlertTitle>{t("governance.source.permissionRequired")}</AlertTitle>
                  <AlertDescription>{t("governance.source.noPermissionRequestDescription")}</AlertDescription>
                </Alert>
              )}
            </section>

            {version.citation_verification.length > 0 && (
              <section className={styles.sheetSection}>
                <h3>{t("governance.source.priorVerification")}</h3>
                {version.citation_verification.map((item) => (
                  <div className={styles.evidenceRecord} key={item.id}>
                    <strong>{item.scope} · {item.resolver_result}</strong>
                    <p className={`${styles.mono} ${styles.muted}`}>{item.verified_identifier} · {item.review_actor}</p>
                  </div>
                ))}
              </section>
            )}

            <form name="sourceReview" autoComplete="off" onSubmit={prepare} className={styles.sheetSection}>
              <h3>{t("governance.source.reviewDecision")}</h3>
              <div className={styles.formGrid}>
                <LabeledSelect id="source-decision" label={t("governance.field.decision")} value={decision} placeholder={t("governance.field.chooseDecision")} options={decisionOptions(t("governance.action.approve"), t("governance.action.reject"))} onChange={setDecision} />
                <LabeledSelect id="source-sensitivity" label={t("governance.review.sensitivity")} value={sensitivity} options={sensitivityOptions(t)} onChange={setSensitivity} />
                {decision === "approve" && <>
                  <LabeledSelect id="source-eligibility" label={t("governance.field.claimEligibility")} value={eligibility} options={eligibilityOptions(t)} onChange={setEligibility} />
                  <LabeledSelect id="source-extraction" label={t("governance.field.extractionPermission")} value={extraction} options={availableExtractionOptions} onChange={setExtraction} disabled={!permissionRequest || !requestHasApprovalCeiling} />
                  <LabeledSelect id="source-storage" label={t("governance.field.storagePermission")} value={storage} options={availableStorageOptions} onChange={setStorage} disabled={!permissionRequest || !requestHasApprovalCeiling} />
                  <LabeledSelect id="source-contradiction" label={t("governance.field.contradictionCheck")} value={contradiction} options={contradictionCheckOptions(t)} onChange={setContradiction} />
                  {extraction === "selected_sections" && (
                    <Field className={styles.full}>
                      <FieldLabel htmlFor="source-scope" className={styles.fieldLabel}>{t("governance.field.scopeLocators")}</FieldLabel>
                      <Textarea id="source-scope" name="extractionScopeLocators" autoComplete="off" className={styles.textarea} value={scope} onChange={(event) => setScope(event.target.value)} required />
                      <FieldDescription>{t("governance.field.onePerLine")}</FieldDescription>
                    </Field>
                  )}
                  <div className={`${styles.full} ${styles.formGrid}`}>
                    <LabeledSelect id="verification-method" label={t("governance.field.verificationMethod")} value={verification.method} options={verificationMethodOptions(t)} onChange={(value) => setVerification({ ...verification, method: value, scope: value === "content_checksum" ? "full_text" : "metadata" })} />
                    <LabeledSelect id="verification-result" label={t("governance.field.resolverResult")} value={verification.resolver_result} options={resolverOptions(t)} onChange={(value) => setVerification({ ...verification, resolver_result: value })} />
                    <Field><FieldLabel htmlFor="verified-identifier" className={styles.fieldLabel}>{t("governance.field.verifiedIdentifier")}</FieldLabel><Input id="verified-identifier" name="verifiedIdentifier" autoComplete="off" className={styles.input} value={verification.verified_identifier} onChange={(event) => setVerification({ ...verification, verified_identifier: event.target.value })} required /></Field>
                    <Field><FieldLabel htmlFor="checked-locator" className={styles.fieldLabel}>{t("governance.field.checkedLocator")}</FieldLabel><Input id="checked-locator" name="checkedLocator" autoComplete="url" className={styles.input} value={verification.checked_locator} onChange={(event) => setVerification({ ...verification, checked_locator: event.target.value })} required /></Field>
                    <Field><FieldLabel htmlFor="resolver-name" className={styles.fieldLabel}>{t("governance.field.resolverName")}</FieldLabel><Input id="resolver-name" name="resolverName" autoComplete="off" className={styles.input} value={verification.resolver_name} onChange={(event) => setVerification({ ...verification, resolver_name: event.target.value })} required /></Field>
                    <Field><FieldLabel htmlFor="verification-notes" className={styles.fieldLabel}>{t("governance.field.notes")}</FieldLabel><Input id="verification-notes" name="verificationNotes" autoComplete="off" className={styles.input} value={verification.notes} onChange={(event) => setVerification({ ...verification, notes: event.target.value })} /></Field>
                  </div>
                </>}
                <Field className={styles.full}>
                  <FieldLabel htmlFor="source-reason" className={styles.fieldLabel}>{t("governance.field.reason")}</FieldLabel>
                  <Textarea id="source-reason" name="sourceReviewReason" autoComplete="off" className={styles.textarea} value={reason} onChange={(event) => setReason(event.target.value)} required />
                </Field>
              </div>
              {decision === "approve" && !permissionRequest && (
                <Alert className={styles.notice}><AlertDescription>{t("governance.source.noPermissionRequestDescription")}</AlertDescription></Alert>
              )}
              {decision === "approve" && permissionRequest && !requestHasApprovalCeiling && (
                <Alert className={styles.notice}><AlertDescription>{t(requestIsNoneNone ? "governance.source.noneRequestDescription" : "governance.source.insufficientRequestDescription")}</AlertDescription></Alert>
              )}
              {decision === "approve" && permissionRequest && (!extractionWithinCeiling || !storageWithinCeiling) && (
                <Alert variant="destructive" className={styles.error}><AlertDescription>{t("governance.source.permissionCeilingExceeded")}</AlertDescription></Alert>
              )}
              {decision === "approve" && !extractionSupportsApproval && (
                <Alert className={styles.notice}><AlertDescription>{t("governance.source.claimExtractionRequired")}</AlertDescription></Alert>
              )}
              {decision === "approve" && permissionRequest && !scopeWithinCeiling && (
                <Alert variant="destructive" className={styles.error}><AlertDescription>{t("governance.source.scopeCeilingExceeded")}</AlertDescription></Alert>
              )}
              {decision === "approve" && !citationVerificationReady && (
                <Alert className={styles.notice}><AlertDescription>{t("governance.source.citationVerificationRequired")}</AlertDescription></Alert>
              )}
              {error && <Alert variant="destructive" className={styles.error}><AlertDescription>{error}</AlertDescription></Alert>}
              <div className={`${styles.actionRow} mt-4`}>
                <span className={styles.muted}>{t("governance.review.noBulk")}</span>
                <Button type="submit" disabled={!decision || !permissionRequest || !requestIdentityMatches || (decision === "approve" && approvalPreparationBlocked)} variant={decision === "reject" ? "destructive" : "default"}>{decision === "approve" ? t("governance.action.prepareApproval") : t("governance.action.prepareRejection")}</Button>
              </div>
            </form>
          </div>
        ) : null}
      </SheetContent>
      <MutationConfirmation
        open={Boolean(command)}
        onOpenChange={(next) => !next && setCommand(null)}
        title={command?.decision === "approve" ? t("governance.confirm.approveSource") : t("governance.confirm.rejectSource")}
        description={t("governance.confirm.immutableDecision")}
        details={command && detail && version ? [
          { label: t("governance.field.sourceId"), value: detail.id },
          { label: t("governance.field.sourceVersionId"), value: command.source_version_id },
          { label: t("governance.field.permissionRequestId"), value: command.source_permission_request_id },
          { label: t("governance.field.contentHash"), value: version.content_hash },
          { label: t("governance.field.decision"), value: command.decision },
          { label: t("governance.review.sensitivity"), value: command.sensitive_information_level },
          { label: t("governance.field.claimEligibility"), value: command.claim_eligibility },
          { label: t("governance.field.extractionPermission"), value: command.extraction_permission },
          { label: t("governance.field.storagePermission"), value: command.storage_permission },
          { label: t("governance.field.contradictionCheck"), value: command.contradicting_evidence_checked },
          { label: t("governance.field.scopeLocators"), value: command.extraction_scope_locators.join(", ") },
          { label: t("governance.field.reason"), value: command.reason },
          ...(command.citation_verification[0] ? [{ label: t("governance.field.verificationMethod"), value: command.citation_verification[0].method }] : []),
          ...(command.citation_verification[0] ? [{ label: t("governance.field.resolverResult"), value: command.citation_verification[0].resolver_result }] : []),
          ...(command.citation_verification[0] ? [{ label: t("governance.field.checkedLocator"), value: command.citation_verification[0].checked_locator }] : []),
          ...(command.citation_verification[0] ? [{ label: t("governance.field.verifiedIdentifier"), value: command.citation_verification[0].verified_identifier }] : []),
        ] : undefined}
        confirmLabel={command?.decision === "approve" ? t("governance.action.approve") : t("governance.action.reject")}
        destructive={command?.decision === "reject"}
        pending={submitting}
        onConfirm={() => void submit()}
      />
      <MutationConfirmation
        open={closeGuard.discardPromptOpen}
        onOpenChange={closeGuard.setDiscardPromptOpen}
        title={t("governance.unsaved.title")}
        description={t("governance.unsaved.description")}
        confirmLabel={t("governance.unsaved.discard")}
        cancelLabel={t("governance.unsaved.keepEditing")}
        destructive
        pending={false}
        onConfirm={closeGuard.confirmDiscard}
      />
    </Sheet>
  )
}

export function ClaimReviewSheet({
  open,
  claimId,
  actor,
  onOpenChange,
  onReviewed,
}: SheetLifecycle & { claimId: string; actor: AuthenticatedActor }) {
  const { t } = useLocale()
  const [detail, setDetail] = useState<ClaimCandidateReviewView | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<ReviewDecision | null>(null)
  const [reason, setReason] = useState("")
  const [confidence, setConfidence] = useState<ConfidenceAssessmentInput>(() => blankConfidence(actor.username))
  const [eligibility, setEligibility] = useState<ClaimEligibility>("eligible_with_limits")
  const [storage, setStorage] = useState<StoragePermission>("derived_knowledge_only")
  const [sensitivity, setSensitivity] = useState<SensitiveInformationLevel>("controlled")
  const [contradictionStatus, setContradictionStatus] = useState<ClaimReviewCommand["contradiction_status"]>("not_checked")
  const [contradictionCheck, setContradictionCheck] = useState<ContradictionCheckStatus>("not_checked")
  const [contradictingIds, setContradictingIds] = useState("")
  const [limitations, setLimitations] = useState("")
  const [exposure, setExposure] = useState<SecretExposureLevel>("general_principle")
  const [command, setCommand] = useState<ClaimReviewCommand | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [baseline, setBaseline] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const { keyFor, clear } = useStableMutationKey(`claim-review:${claimId}`)

  useEffect(() => {
    let active = true
    governanceApi.claim(claimId).then(
      (value) => {
        if (!active) return
        setDetail(value)
        setSensitivity(value.sensitivity)
        setLimitations(value.proposed_limitations.join("\n"))
        setBaseline(draftSignature({
          decision: null,
          reason: "",
          confidence: blankConfidence(actor.username),
          eligibility: "eligible_with_limits",
          storage: "derived_knowledge_only",
          sensitivity: value.sensitivity,
          contradictionStatus: "not_checked",
          contradictionCheck: "not_checked",
          contradictingIds: "",
          limitations: value.proposed_limitations.join("\n"),
          exposure: "general_principle",
        }))
        setLoading(false)
      },
      (cause) => { if (active) { setError(errorText(cause, t("governance.error.generic"))); setLoading(false) } }
    )
    return () => { active = false }
  }, [actor.username, claimId, t])

  const currentDraft = draftSignature({ decision, reason, confidence, eligibility, storage, sensitivity, contradictionStatus, contradictionCheck, contradictingIds, limitations, exposure })
  const dirty = open && !submitted && baseline !== null && currentDraft !== baseline
  const closeGuard = useGuardedOpenChange({
    dirty,
    message: t("governance.unsaved.browserWarning"),
    onOpenChange,
  })

  const prepare = (event: FormEvent) => {
    event.preventDefault()
    if (!decision) return
    const next: ClaimReviewCommand = decision === "reject" ? {
      decision,
      reason,
      confidence: null,
      claim_eligibility: "ineligible",
      storage_permission: "none",
      sensitive_information_level: sensitivity,
      contradiction_status: "not_checked",
      contradicting_evidence_checked: "not_checked",
      contradicting_evidence_ids: [],
      limitations: lines(limitations),
      secret_exposure_level: exposure,
    } : {
      decision,
      reason,
      confidence,
      claim_eligibility: eligibility,
      storage_permission: storage,
      sensitive_information_level: sensitivity,
      contradiction_status: contradictionStatus,
      contradicting_evidence_checked: contradictionCheck,
      contradicting_evidence_ids: lines(contradictingIds),
      limitations: lines(limitations),
      secret_exposure_level: exposure,
    }
    setCommand(next)
  }

  const submit = async () => {
    if (!command) return
    setSubmitting(true)
    setError(null)
    try {
      await governanceApi.reviewClaim(claimId, command, keyFor(command))
      clear(); setCommand(null); setSubmitted(true); onReviewed()
    } catch (cause) {
      setError(errorText(cause, t("governance.error.generic"))); setCommand(null)
    } finally { setSubmitting(false) }
  }

  return (
    <Sheet open={open} onOpenChange={closeGuard.guardedOnOpenChange}>
      <SheetContent className={styles.sheet}>
        <SheetHeader className={styles.sheetHeader}>
          <SheetTitle className={styles.sheetTitle}>{t("governance.claim.title")}</SheetTitle>
          <SheetDescription>{t("governance.claim.description")}</SheetDescription>
        </SheetHeader>
        {loading ? <LoadingDetail /> : detail ? (
          <div className={styles.sheetScroll}>
            <section className={styles.sheetSection}>
              <h3>{t("governance.claim.extractedClaim")}</h3>
              <p className={styles.excerpt}>{detail.claim}</p>
              <div className={`${styles.detailGrid} mt-4`}>
                <DetailValue label={t("governance.field.claimId")}>{detail.id}</DetailValue>
                <DetailValue label={t("governance.field.sourceVersionId")}>{detail.source_version_id}</DetailValue>
                <DetailValue label={t("governance.field.candidateChecksum")}>{detail.candidate_checksum}</DetailValue>
                <DetailValue label={t("governance.field.schema")}>{detail.candidate_schema_version}</DetailValue>
                <DetailValue label={t("governance.field.claimRole")}>{detail.claim_role}</DetailValue>
                <DetailValue label={t("governance.field.evidenceClass")}>{detail.proposed_evidence_class}</DetailValue>
                <DetailValue label={t("governance.field.locator")}>{detail.locator.source_locator}</DetailValue>
                <DetailValue label={t("governance.field.extractionConfidence")}>{Math.round(detail.extraction_confidence * 100)}%</DetailValue>
              </div>
              <div className={`${styles.evidenceRecord} mt-4`}><strong>{t("governance.claim.evidenceExcerpt")}</strong><p>{detail.evidence_excerpt}</p></div>
            </section>
            <section className={styles.sheetSection}>
              <h3>{t("governance.claim.sourceContext")}</h3>
              <div className={styles.detailGrid}>
                <DetailValue label={t("governance.term.source")}>{detail.source_context.title}</DetailValue>
                <DetailValue label={t("governance.field.citationStatus")}>{detail.source_context.citation_status}</DetailValue>
                <DetailValue label={t("governance.field.contentAccess")}>{detail.source_context.content_access}</DetailValue>
                <DetailValue label={t("governance.field.doi")}>{detail.source_context.citation.doi || "—"}</DetailValue>
              </div>
              <div className={`${styles.sourceContent} mt-4`}>{detail.source_context.content}</div>
            </section>
            <form name="claimReview" autoComplete="off" onSubmit={prepare} className={styles.sheetSection}>
              <h3>{t("governance.claim.reviewDecision")}</h3>
              <div className={styles.formGrid}>
                <LabeledSelect id="claim-decision" label={t("governance.field.decision")} value={decision} placeholder={t("governance.field.chooseDecision")} options={decisionOptions(t("governance.action.approve"), t("governance.action.reject"))} onChange={setDecision} />
                <LabeledSelect id="claim-sensitivity" label={t("governance.review.sensitivity")} value={sensitivity} options={sensitivityOptions(t)} onChange={setSensitivity} />
                {decision === "approve" && <>
                  <LabeledSelect id="claim-eligibility" label={t("governance.field.claimEligibility")} value={eligibility} options={eligibilityOptions(t)} onChange={setEligibility} />
                  <LabeledSelect id="claim-storage" label={t("governance.field.storagePermission")} value={storage} options={storageOptions(t)} onChange={setStorage} />
                  <LabeledSelect id="claim-contradiction-status" label={t("governance.field.contradictionStatus")} value={contradictionStatus} options={contradictionStatusOptions(t)} onChange={setContradictionStatus} />
                  <LabeledSelect id="claim-contradiction-check" label={t("governance.field.contradictionCheck")} value={contradictionCheck} options={contradictionCheckOptions(t)} onChange={setContradictionCheck} />
                  <LabeledSelect id="claim-exposure" label={t("governance.field.secretExposure")} value={exposure} options={exposureOptions(t)} onChange={setExposure} />
                  <Field><FieldLabel htmlFor="claim-conflicts" className={styles.fieldLabel}>{t("governance.field.contradictingEvidence")}</FieldLabel><Textarea id="claim-conflicts" name="contradictingEvidenceIds" autoComplete="off" className={styles.textarea} value={contradictingIds} onChange={(event) => setContradictingIds(event.target.value)} /><FieldDescription>{t("governance.field.onePerLine")}</FieldDescription></Field>
                  <ConfidenceEditor value={confidence} onChange={setConfidence} />
                </>}
                <Field className={styles.full}><FieldLabel htmlFor="claim-limitations" className={styles.fieldLabel}>{t("governance.field.limitations")}</FieldLabel><Textarea id="claim-limitations" name="claimLimitations" autoComplete="off" className={styles.textarea} value={limitations} onChange={(event) => setLimitations(event.target.value)} required={decision === "approve"} /><FieldDescription>{t("governance.field.onePerLine")}</FieldDescription></Field>
                <Field className={styles.full}><FieldLabel htmlFor="claim-reason" className={styles.fieldLabel}>{t("governance.field.reason")}</FieldLabel><Textarea id="claim-reason" name="claimReviewReason" autoComplete="off" className={styles.textarea} value={reason} onChange={(event) => setReason(event.target.value)} required /></Field>
              </div>
              {error && <Alert variant="destructive" className={styles.error}><AlertDescription>{error}</AlertDescription></Alert>}
              <div className={`${styles.actionRow} mt-4`}><span className={styles.muted}>{t("governance.review.noBulk")}</span><Button type="submit" disabled={!decision} variant={decision === "reject" ? "destructive" : "default"}>{decision === "approve" ? t("governance.action.prepareApproval") : t("governance.action.prepareRejection")}</Button></div>
            </form>
          </div>
        ) : <Alert variant="destructive" className={styles.error}><AlertDescription>{error}</AlertDescription></Alert>}
      </SheetContent>
      <MutationConfirmation
        open={Boolean(command)}
        onOpenChange={(next) => !next && setCommand(null)}
        title={command?.decision === "approve" ? t("governance.confirm.approveClaim") : t("governance.confirm.rejectClaim")}
        description={t("governance.confirm.evidenceDecision")}
        details={command && detail ? [
          { label: t("governance.field.claimId"), value: detail.id },
          { label: t("governance.field.sourceVersionId"), value: detail.source_version_id },
          { label: t("governance.field.candidateChecksum"), value: detail.candidate_checksum },
          { label: t("governance.field.claimRole"), value: detail.claim_role },
          { label: t("governance.field.evidenceClass"), value: detail.proposed_evidence_class },
          { label: t("governance.field.knowledgeOrigin"), value: detail.source_context.knowledge_origin },
          { label: t("governance.field.locator"), value: detail.locator.source_locator },
          { label: t("governance.field.decision"), value: command.decision },
          { label: t("governance.review.sensitivity"), value: command.sensitive_information_level },
          { label: t("governance.field.claimEligibility"), value: command.claim_eligibility },
          { label: t("governance.field.storagePermission"), value: command.storage_permission },
          { label: t("governance.field.contradictionStatus"), value: command.contradiction_status },
          { label: t("governance.field.contradictionCheck"), value: command.contradicting_evidence_checked },
          { label: t("governance.field.secretExposure"), value: command.secret_exposure_level },
          { label: t("governance.field.contradictingEvidence"), value: command.contradicting_evidence_ids.join(", ") },
          { label: t("governance.field.limitations"), value: command.limitations.join(" · ") },
          { label: t("governance.field.reason"), value: command.reason },
          ...confidenceConfirmationDetails(command.confidence, t),
        ] : undefined}
        confirmLabel={command?.decision === "approve" ? t("governance.action.approve") : t("governance.action.reject")}
        destructive={command?.decision === "reject"}
        pending={submitting}
        onConfirm={() => void submit()}
      />
      <MutationConfirmation
        open={closeGuard.discardPromptOpen}
        onOpenChange={closeGuard.setDiscardPromptOpen}
        title={t("governance.unsaved.title")}
        description={t("governance.unsaved.description")}
        confirmLabel={t("governance.unsaved.discard")}
        cancelLabel={t("governance.unsaved.keepEditing")}
        destructive
        pending={false}
        onConfirm={closeGuard.confirmDiscard}
      />
    </Sheet>
  )
}

export function MappingReviewSheet({
  open,
  mappingId,
  actor,
  onOpenChange,
  onReviewed,
}: SheetLifecycle & { mappingId: string; actor: AuthenticatedActor }) {
  const { t } = useLocale()
  const [detail, setDetail] = useState<MappingProposalDetail | null>(null)
  const [evidence, setEvidence] = useState<Record<string, EvidenceVersionView | null>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<ReviewDecision | null>(null)
  const [reason, setReason] = useState("")
  const [confidence, setConfidence] = useState<ConfidenceAssessmentInput>(() => blankConfidence(actor.username))
  const [resolution, setResolution] = useState<CanonicalResolution | null>(null)
  const [canonicalQuery, setCanonicalQuery] = useState("")
  const [canonicalResults, setCanonicalResults] = useState<CanonicalEntitySummary[]>([])
  const [canonicalId, setCanonicalId] = useState<string | null>(null)
  const [searching, setSearching] = useState(false)
  const [command, setCommand] = useState<MappingReviewCommand | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [baseline, setBaseline] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const { keyFor, clear } = useStableMutationKey(`mapping-review:${mappingId}`)

  useEffect(() => {
    let active = true
    governanceApi.mapping(mappingId).then(async (value) => {
      if (!active) return
      setDetail(value)
      setBaseline(draftSignature({
        decision: null,
        reason: "",
        confidence: blankConfidence(actor.username),
        resolution: null,
        canonicalQuery: "",
        canonicalId: null,
      }))
      const records = await Promise.all(value.supporting_evidence_version_ids.map(async (id) => {
        try {
          const response = await governanceApi.evidenceVersion(id)
          return [id, response.items[0] ?? null] as const
        } catch {
          return [id, null] as const
        }
      }))
      if (active) { setEvidence(Object.fromEntries(records)); setLoading(false) }
    }, (cause) => { if (active) { setError(errorText(cause, t("governance.error.generic"))); setLoading(false) } })
    return () => { active = false }
  }, [actor.username, mappingId, t])

  const isEntity = detail?.kind === "entity"
  const proposal = detail?.proposal
  const entityType = isEntity && proposal && "entity" in proposal ? proposal.entity.type : undefined
  const evidenceUnavailable = Boolean(detail?.supporting_evidence_version_ids.some((id) => evidence[id] === null || evidence[id] === undefined))
  const validationFailed = Boolean(detail?.validation_runs.some((run) => !run.passed))
  const approvalBlocked = evidenceUnavailable || validationFailed
  const currentDraft = draftSignature({ decision, reason, confidence, resolution, canonicalQuery, canonicalId })
  const dirty = open && !submitted && baseline !== null && currentDraft !== baseline
  const closeGuard = useGuardedOpenChange({
    dirty,
    message: t("governance.unsaved.browserWarning"),
    onOpenChange,
  })

  const searchCanonical = async () => {
    setSearching(true); setError(null); setCanonicalId(null); setCanonicalResults([])
    try {
      const response = await governanceApi.canonicalEntities(canonicalQuery, entityType)
      setCanonicalResults(response.items)
    } catch (cause) { setError(errorText(cause, t("governance.error.generic"))) }
    finally { setSearching(false) }
  }

  const prepare = (event: FormEvent) => {
    event.preventDefault()
    if (!decision || (decision === "approve" && approvalBlocked)) return
    if (decision === "approve" && isEntity && (!resolution || (resolution !== "create" && !canonicalId))) return
    const next: MappingReviewCommand = decision === "reject" ? {
      decision,
      reason,
      confidence: null,
      canonical_resolution: null,
      canonical_entity_id: null,
    } : {
      decision,
      reason,
      confidence,
      canonical_resolution: isEntity ? resolution : null,
      canonical_entity_id: isEntity && resolution !== "create" ? canonicalId : null,
    }
    setCommand(next)
  }

  const submit = async () => {
    if (!command) return
    setSubmitting(true); setError(null)
    try {
      await governanceApi.reviewMapping(mappingId, command, keyFor(command))
      clear(); setCommand(null); setSubmitted(true); onReviewed()
    } catch (cause) { setError(errorText(cause, t("governance.error.generic"))); setCommand(null) }
    finally { setSubmitting(false) }
  }

  const evidenceRecords = useMemo(() => Object.entries(evidence), [evidence])

  return (
    <Sheet open={open} onOpenChange={closeGuard.guardedOnOpenChange}>
      <SheetContent className={styles.sheet}>
        <SheetHeader className={styles.sheetHeader}>
          <SheetTitle className={styles.sheetTitle}>{t("governance.mapping.title")}</SheetTitle>
          <SheetDescription>{t("governance.mapping.description")}</SheetDescription>
        </SheetHeader>
        {loading ? <LoadingDetail /> : detail && proposal ? (
          <div className={styles.sheetScroll}>
            <section className={styles.sheetSection}>
              <h3>{t("governance.mapping.proposal")}</h3>
              <p className={styles.excerpt}>{detail.subject}</p>
              <div className={`${styles.detailGrid} mt-4`}>
                <DetailValue label={t("governance.field.mappingId")}>{detail.id}</DetailValue>
                <DetailValue label={t("governance.field.proposalChecksum")}>{detail.proposal_checksum}</DetailValue>
                <DetailValue label={t("governance.field.schema")}>{detail.schema_version}</DetailValue>
                <DetailValue label={t("governance.review.type")}>{detail.kind}</DetailValue>
                <DetailValue label={t("governance.review.status")}><StatusBadge status={detail.status} /></DetailValue>
                <DetailValue label={t("governance.field.ontology")}>{proposal.ontology_paths.join(", ")}</DetailValue>
                <DetailValue label={t("governance.field.knowledgeOrigin")}>{proposal.knowledge_origin}</DetailValue>
              </div>
              {"entity" in proposal ? (
                <div className={`${styles.evidenceRecord} mt-4`}>
                  <strong>{proposal.entity.name} · {proposal.entity.type}</strong>
                  <p>{proposal.definition}</p>
                  <p className={`${styles.mono} ${styles.muted}`}>{t("governance.field.aliases")}: {proposal.entity.aliases.join(", ") || "—"}</p>
                  <p className={`${styles.microLabel} mt-3`}>{t("governance.field.attributes")}</p>
                  <pre className={styles.jsonDetail}>{JSON.stringify(proposal.entity.attributes, null, 2)}</pre>
                </div>
              ) : (
                <div className={`${styles.evidenceRecord} mt-4`}>
                  <strong>{proposal.relation_type}</strong>
                  <p>{proposal.assertion}</p>
                  <p className={`${styles.mono} ${styles.muted}`}>{t("governance.field.sourceEntityId")}: {proposal.source_entity_id}</p>
                  <p className={`${styles.mono} ${styles.muted}`}>{t("governance.field.targetEntityId")}: {proposal.target_entity_id}</p>
                </div>
              )}
              <div className={`${styles.evidenceRecord} mt-4`}><strong>{t("governance.claim.evidenceExcerpt")}</strong><p>{proposal.evidence_excerpt}</p><p className={`${styles.mono} ${styles.muted}`}>{proposal.source_locator}</p></div>
            </section>

            <section className={styles.sheetSection}>
              <h3>{t("governance.mapping.supportingEvidence")}</h3>
              {evidenceRecords.map(([id, record]) => (
                <div className={styles.evidenceRecord} key={id}>
                  <strong>{record?.claim || t("governance.mapping.evidenceUnavailable")}</strong>
                  <p className={`${styles.mono} ${styles.muted}`}>{t("governance.field.evidenceVersionId")}: {id}</p>
                  <p>{record?.evidence_class || t("governance.term.evidenceCard")} · {record ? `${Math.round(record.confidence_score * 100)}%` : shortId(id)}</p>
                  {record?.source_locator && <p className={`${styles.mono} ${styles.muted}`}>{record.source_locator}</p>}
                  {record?.limitations.map((item) => <p key={item} className={styles.muted}>— {item}</p>)}
                </div>
              ))}
            </section>

            {detail.validation_runs.length > 0 && (
              <section className={styles.sheetSection}>
                <h3>{t("governance.mapping.validationRuns")}</h3>
                {detail.validation_runs.map((run) => <div key={run.id} className={styles.evidenceRecord}>{run.passed ? <CheckCircle2Icon className={`size-4 ${styles.validationPass}`} aria-hidden="true" /> : <ShieldXIcon className={`size-4 ${styles.validationFail}`} aria-hidden="true" />}<strong>{run.phase}</strong><p className={`${styles.mono} ${styles.muted}`}>{run.id} · {run.rule_version}</p><pre className={styles.jsonDetail}>{JSON.stringify(run.results, null, 2)}</pre></div>)}
              </section>
            )}

            <form name="mappingReview" autoComplete="off" onSubmit={prepare} className={styles.sheetSection}>
              <h3>{t("governance.mapping.reviewDecision")}</h3>
              <div className={styles.formGrid}>
                <LabeledSelect id="mapping-decision" label={t("governance.field.decision")} value={decision} placeholder={t("governance.field.chooseDecision")} options={decisionOptions(t("governance.action.approve"), t("governance.action.reject"))} onChange={setDecision} />
                {decision === "approve" && isEntity && <LabeledSelect id="canonical-resolution" label={t("governance.field.canonicalResolution")} value={resolution} placeholder={t("governance.field.chooseResolution")} options={canonicalResolutionOptions(t)} onChange={(value) => { setResolution(value); if (value === "create") setCanonicalId(null) }} />}
                {decision === "approve" && isEntity && resolution !== "create" && (
                  <div className={`${styles.full} ${styles.sheetSection}`}>
                    <h3>{t("governance.mapping.canonicalRegistry")}</h3>
                    <div className={styles.toolbar}>
                      <Input name="canonicalEntityQuery" autoComplete="off" className={styles.input} value={canonicalQuery} onChange={(event) => setCanonicalQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void searchCanonical() } }} placeholder={t("governance.mapping.searchCanonical")} aria-label={t("governance.mapping.searchCanonical")} />
                      <Button type="button" variant="outline" onClick={() => void searchCanonical()} disabled={searching}><SearchIcon aria-hidden="true" /> {searching ? t("governance.action.searching") : t("governance.action.search")}</Button>
                    </div>
                    <div className="mt-3 space-y-2">
                      {canonicalResults.map((entity) => (
                        <label key={entity.id} className={styles.artifactRow}>
                          <input
                            type="radio"
                            name="canonicalEntityId"
                            value={entity.id}
                            checked={canonicalId === entity.id}
                            onChange={() => setCanonicalId(entity.id)}
                            className="mt-1 size-4 accent-[var(--module-accent)]"
                          />
                          <span><strong className={styles.artifactTitle}>{entity.canonical_name}</strong><span className={`${styles.mono} ${styles.muted}`}>{entity.entity_type} · {entity.canonical_key}</span></span>
                          <Badge variant="outline">v{entity.latest_version}</Badge>
                        </label>
                      ))}
                    </div>
                  </div>
                )}
                {decision === "approve" && <ConfidenceEditor value={confidence} onChange={setConfidence} />}
                <Field className={styles.full}><FieldLabel htmlFor="mapping-reason" className={styles.fieldLabel}>{t("governance.field.reason")}</FieldLabel><Textarea id="mapping-reason" name="mappingReviewReason" autoComplete="off" className={styles.textarea} value={reason} onChange={(event) => setReason(event.target.value)} required /></Field>
              </div>
              {error && <Alert variant="destructive" className={styles.error}><AlertDescription>{error}</AlertDescription></Alert>}
              {approvalBlocked && <Alert variant="destructive" className={styles.error}><AlertTitle>{t("governance.mapping.approvalBlocked")}</AlertTitle><AlertDescription>{evidenceUnavailable ? t("governance.mapping.evidenceFetchBlocked") : t("governance.mapping.validationBlocked")}</AlertDescription></Alert>}
              <div className={`${styles.actionRow} mt-4`}><span className={styles.muted}>{t("governance.mapping.entailmentGate")}</span><Button type="submit" disabled={!decision || (decision === "approve" && (approvalBlocked || (isEntity && (!resolution || (resolution !== "create" && !canonicalId)))))} variant={decision === "reject" ? "destructive" : "default"}>{decision === "approve" ? t("governance.action.prepareApproval") : t("governance.action.prepareRejection")}</Button></div>
            </form>
          </div>
        ) : <Alert variant="destructive" className={styles.error}><AlertDescription>{error}</AlertDescription></Alert>}
      </SheetContent>
      <MutationConfirmation
        open={Boolean(command)}
        onOpenChange={(next) => !next && setCommand(null)}
        title={command?.decision === "approve" ? t("governance.confirm.approveMapping") : t("governance.confirm.rejectMapping")}
        description={t("governance.confirm.mappingDecision")}
        details={command && detail ? [
          { label: t("governance.field.mappingId"), value: detail.id },
          { label: t("governance.field.proposalChecksum"), value: detail.proposal_checksum },
          { label: t("governance.review.subject"), value: detail.subject },
          { label: t("governance.review.type"), value: detail.kind },
          { label: t("governance.field.schema"), value: detail.schema_version },
          { label: t("governance.field.decision"), value: command.decision },
          { label: t("governance.review.sensitivity"), value: detail.sensitivity },
          { label: t("governance.mapping.supportingEvidence"), value: detail.supporting_evidence_version_ids.join(", ") },
          { label: t("governance.field.reason"), value: command.reason },
          ...(detail.proposal && "entity" in detail.proposal ? [
            { label: t("governance.review.type"), value: detail.proposal.entity.type },
            { label: t("governance.field.domainId"), value: detail.proposal.entity.id },
          ] : [
            { label: t("governance.field.sourceEntityId"), value: detail.proposal.source_entity_id },
            { label: t("governance.field.targetEntityId"), value: detail.proposal.target_entity_id },
          ]),
          ...(command.canonical_resolution ? [{ label: t("governance.field.canonicalResolution"), value: command.canonical_resolution }] : []),
          ...(command.canonical_entity_id ? [{ label: t("governance.confirm.canonicalEntityId"), value: command.canonical_entity_id }] : []),
          ...confidenceConfirmationDetails(command.confidence, t),
        ] : undefined}
        confirmLabel={command?.decision === "approve" ? t("governance.action.approve") : t("governance.action.reject")}
        destructive={command?.decision === "reject"}
        pending={submitting}
        onConfirm={() => void submit()}
      />
      <MutationConfirmation
        open={closeGuard.discardPromptOpen}
        onOpenChange={closeGuard.setDiscardPromptOpen}
        title={t("governance.unsaved.title")}
        description={t("governance.unsaved.description")}
        confirmLabel={t("governance.unsaved.discard")}
        cancelLabel={t("governance.unsaved.keepEditing")}
        destructive
        pending={false}
        onConfirm={closeGuard.confirmDiscard}
      />
    </Sheet>
  )
}

type Translator = ReturnType<typeof useLocale>["t"]

function sensitivityOptions(t: Translator) {
  return [
    { value: "public" as const, label: t("governance.enum.public") },
    { value: "controlled" as const, label: t("governance.enum.controlled") },
    { value: "secret_method" as const, label: t("governance.enum.secretMethod") },
    { value: "restricted" as const, label: t("governance.enum.restricted") },
  ]
}
function eligibilityOptions(t: Translator) {
  return [
    { value: "eligible" as const, label: t("governance.enum.eligible") },
    { value: "eligible_with_limits" as const, label: t("governance.enum.eligibleWithLimits") },
    { value: "ineligible" as const, label: t("governance.enum.ineligible") },
  ]
}
function extractionOptions(t: Translator) {
  return [
    { value: "selected_sections" as const, label: t("governance.enum.selectedSections") },
    { value: "full_text" as const, label: t("governance.enum.fullText") },
    { value: "metadata_only" as const, label: t("governance.enum.metadataOnly") },
    { value: "none" as const, label: t("governance.enum.none") },
  ]
}
function storageOptions(t: Translator) {
  return [
    { value: "derived_knowledge_only" as const, label: t("governance.enum.derivedOnly") },
    { value: "derived_with_short_excerpt" as const, label: t("governance.enum.derivedExcerpt") },
    { value: "none" as const, label: t("governance.enum.none") },
  ]
}
function contradictionCheckOptions(t: Translator) {
  return [
    { value: "checked_none_found" as const, label: t("governance.enum.checkedNone") },
    { value: "checked_conflicts_linked" as const, label: t("governance.enum.checkedLinked") },
    { value: "not_applicable" as const, label: t("governance.enum.notApplicable") },
    { value: "not_checked" as const, label: t("governance.enum.notChecked") },
  ]
}
function contradictionStatusOptions(t: Translator) {
  return [
    { value: "none_found" as const, label: t("governance.enum.noneFound") },
    { value: "resolved" as const, label: t("governance.enum.resolved") },
    { value: "unresolved" as const, label: t("governance.enum.unresolved") },
    { value: "not_checked" as const, label: t("governance.enum.notChecked") },
  ]
}
function exposureOptions(t: Translator) {
  return [
    { value: "none" as const, label: t("governance.enum.none") },
    { value: "general_principle" as const, label: t("governance.enum.generalPrinciple") },
    { value: "method_detail" as const, label: t("governance.enum.methodDetail") },
    { value: "operational_secret" as const, label: t("governance.enum.operationalSecret") },
  ]
}
function verificationMethodOptions(t: Translator) {
  return [
    { value: "doi_resolver" as const, label: t("governance.enum.doiResolver") },
    { value: "canonical_locator" as const, label: t("governance.enum.canonicalLocator") },
    { value: "manual_metadata" as const, label: t("governance.enum.manualMetadata") },
    { value: "content_checksum" as const, label: t("governance.enum.contentChecksum") },
  ]
}
function resolverOptions(t: Translator) {
  return [
    { value: "matched" as const, label: t("governance.enum.matched") },
    { value: "mismatch" as const, label: t("governance.enum.mismatch") },
    { value: "not_found" as const, label: t("governance.enum.notFound") },
    { value: "error" as const, label: t("governance.enum.error") },
  ]
}
function canonicalResolutionOptions(t: Translator) {
  return [
    { value: "create" as const, label: t("governance.enum.create") },
    { value: "reuse" as const, label: t("governance.enum.reuse") },
    { value: "merge" as const, label: t("governance.enum.merge") },
  ]
}
