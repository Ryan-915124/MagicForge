"use client"

import { useEffect, useState } from "react"
import {
  BookOpenCheckIcon,
  FileCheck2Icon,
  QuoteIcon,
  RefreshCwIcon,
  ScanLineIcon,
  SearchCheckIcon,
  WaypointsIcon,
} from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { AuthenticatedActor } from "@/lib/api/types"
import { governanceApi } from "@/lib/api/governance-client"
import type {
  ClaimCandidateSummary,
  MappingProposalSummary,
  SourceReviewQueueItem,
  WorkflowStatus,
} from "@/lib/api/governance-types"

import { LabeledSelect, StatusBadge, errorText, shortId } from "./governance-common"
import { ClaimReviewSheet, MappingReviewSheet, SourceReviewSheet } from "./review-sheets"
import {
  boundedPageOffset,
  updateGovernanceUrl,
  useGovernanceSearchParams,
} from "./governance-url-state"
import styles from "./governance.module.css"

type QueueKind = "source" | "claim" | "mapping"
type QueueItem = SourceReviewQueueItem | ClaimCandidateSummary | MappingProposalSummary
type SelectedItem = { kind: QueueKind; id: string; sourceVersionId?: string } | null
const PAGE_SIZE = 50

function QueueLoading({ label }: { label: string }) {
  return (
    <div className={styles.queueLoading} role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      {Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-11 w-full" aria-hidden="true" />)}
    </div>
  )
}

const queueIcons = {
  source: BookOpenCheckIcon,
  claim: QuoteIcon,
  mapping: WaypointsIcon,
} as const

export function ReviewDesk({ actor }: { actor: AuthenticatedActor }) {
  const { t } = useLocale()
  const searchParams = useGovernanceSearchParams()
  const requestedKind = searchParams.get("queue")
  const kind: QueueKind = requestedKind === "claim" || requestedKind === "mapping" ? requestedKind : "source"
  const requestedStatus = searchParams.get("review_status")
  const status: WorkflowStatus | "all" = requestedStatus === "approved" || requestedStatus === "rejected" || requestedStatus === "all" ? requestedStatus : "submitted"
  const offset = boundedPageOffset(searchParams.get("review_offset"), PAGE_SIZE)
  const requestedSelectedKind = searchParams.get("review_type")
  const selectedKind: QueueKind = requestedSelectedKind === "claim" || requestedSelectedKind === "mapping" ? requestedSelectedKind : "source"
  const selectedId = searchParams.get("review_id")
  const selectedSourceVersion = searchParams.get("source_version")
  const selected: SelectedItem = selectedId
    ? { kind: selectedKind, id: selectedId, sourceVersionId: selectedSourceVersion ?? undefined }
    : null
  const [items, setItems] = useState<QueueItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    const patch: Record<string, string | number | null> = {}
    if (requestedKind !== kind) patch.queue = kind
    if (requestedStatus !== status) patch.review_status = status
    if (searchParams.get("review_offset") !== String(offset) && offset !== 0) patch.review_offset = offset
    if (searchParams.get("review_offset") && offset === 0) patch.review_offset = null
    if (selectedId && requestedSelectedKind !== selectedKind) patch.review_type = selectedKind
    if (Object.keys(patch).length > 0) updateGovernanceUrl(patch, "replace")
  }, [kind, offset, requestedKind, requestedSelectedKind, requestedStatus, searchParams, selectedId, selectedKind, status])

  useEffect(() => {
    let active = true
    const filter = status === "all" ? null : status
    const pending = kind === "source"
      ? governanceApi.sourceQueue(filter, offset, PAGE_SIZE)
      : kind === "claim"
        ? governanceApi.claimQueue(filter, offset, PAGE_SIZE)
        : governanceApi.mappingQueue(filter, undefined, offset, PAGE_SIZE)
    pending.then(
      (response) => {
        if (!active) return
        setItems(response.items)
        setLoading(false)
      },
      (cause: unknown) => {
        if (!active) return
        setError(errorText(cause, t("governance.error.generic")))
        setItems([])
        setLoading(false)
      }
    )
    return () => { active = false }
  }, [kind, offset, reloadToken, status, t])

  const refresh = () => {
    setLoading(true)
    setError(null)
    setReloadToken((value) => value + 1)
  }

  const statusOptions = [
    { value: "submitted" as const, label: t("governance.status.submitted") },
    { value: "approved" as const, label: t("governance.status.approved") },
    { value: "rejected" as const, label: t("governance.status.rejected") },
    { value: "all" as const, label: t("governance.status.all") },
  ]

  const reviewed = () => {
    updateGovernanceUrl({ review_id: null, review_type: null, source_version: null }, "replace")
    refresh()
  }

  return (
    <section aria-labelledby="review-desk-title">
      <div className={styles.sectionHeader}>
        <div>
          <p className={styles.eyebrow}>{t("governance.review.eyebrow")}</p>
          <h2 id="review-desk-title" className={styles.sectionTitle}>{t("governance.review.title")}</h2>
          <p className={styles.sectionDescription}>{t("governance.review.description")}</p>
        </div>
        <div className={styles.reviewProtocol}>
          <ScanLineIcon aria-hidden="true" />
          <span>{t("governance.review.noBulk")}</span>
        </div>
      </div>

      <div className={styles.instrumentBar}>
        <Tabs value={kind} onValueChange={(value) => { setLoading(true); setError(null); updateGovernanceUrl({ queue: value, review_offset: null, review_id: null, review_type: null, source_version: null }, "push") }}>
          <TabsList variant="line" className={styles.queueTabs}>
            <TabsTrigger value="source" className={styles.queueTab}><BookOpenCheckIcon aria-hidden="true" />{t("governance.term.source")}</TabsTrigger>
            <TabsTrigger value="claim" className={styles.queueTab}><QuoteIcon aria-hidden="true" />{t("governance.term.claim")}</TabsTrigger>
            <TabsTrigger value="mapping" className={styles.queueTab}><WaypointsIcon aria-hidden="true" />{t("governance.term.mapping")}</TabsTrigger>
          </TabsList>
        </Tabs>
        <div className={styles.toolbar}>
          <LabeledSelect
            id="review-status"
            label={t("governance.review.statusFilter")}
            value={status}
            options={statusOptions}
            onChange={(value) => { setLoading(true); setError(null); updateGovernanceUrl({ review_status: value, review_offset: null, review_id: null, review_type: null, source_version: null }, "push") }}
          />
          <Button variant="outline" onClick={refresh} disabled={loading}>
            <RefreshCwIcon data-icon="inline-start" aria-hidden="true" /> {t("governance.action.refresh")}
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="destructive" className={styles.error}>
          <AlertTitle>{t("governance.error.queue")}</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className={styles.queuePanel} data-queue={kind}>
        <div className={styles.ledgerHeader}>
          <div>
            <span className={styles.microLabel}>{t("governance.review.currentRegister")}</span>
            <strong>{kind === "source" ? t("governance.term.source") : kind === "claim" ? t("governance.term.claim") : t("governance.term.mapping")}</strong>
          </div>
          <span className={styles.ledgerCount}>{t("governance.review.visibleRecords", { count: items.length })}</span>
        </div>
        {loading ? <QueueLoading label={t("governance.review.loadingQueue")} /> : items.length === 0 ? (
          <Empty className={styles.empty}>
            <EmptyHeader>
              <EmptyMedia variant="icon"><FileCheck2Icon aria-hidden="true" /></EmptyMedia>
              <EmptyTitle>{t("governance.review.empty")}</EmptyTitle>
              <EmptyDescription>{t("governance.review.emptyDescription")}</EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <Table className={styles.queueTable}>
            <TableHeader>
              <TableRow>
                <TableHead>{t("governance.review.subject")}</TableHead>
                <TableHead>{t("governance.review.type")}</TableHead>
                <TableHead>{t("governance.review.sensitivity")}</TableHead>
                <TableHead>{t("governance.review.status")}</TableHead>
                <TableHead className="text-right">{t("governance.review.inspect")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((item, index) => {
                const id = kind === "source"
                  ? (item as SourceReviewQueueItem).source_id
                  : (item as ClaimCandidateSummary | MappingProposalSummary).id
                const subject = kind === "source"
                  ? (item as SourceReviewQueueItem).title
                  : kind === "claim"
                    ? (item as ClaimCandidateSummary).claim
                    : (item as MappingProposalSummary).subject
                const type = kind === "source"
                  ? (item as SourceReviewQueueItem).source_type
                  : kind === "claim"
                    ? (item as ClaimCandidateSummary).proposed_evidence_class
                    : (item as MappingProposalSummary).kind
                const QueueIcon = queueIcons[kind]
                return (
                  <TableRow className={styles.queueRow} key={kind === "source" ? (item as SourceReviewQueueItem).source_version_id : id}>
                    <TableCell>
                      <div className={styles.subjectCell}>
                        <span className={styles.queueIndex} aria-hidden="true">{String(offset + index + 1).padStart(2, "0")}</span>
                        <span className={styles.queueGlyph} aria-hidden="true"><QueueIcon /></span>
                        <span className={styles.subjectCopy}>
                          <span className={styles.queueSubject} title={subject}>{subject}</span>
                          <span className={`${styles.mono} ${styles.muted}`}>{shortId(id)}</span>
                        </span>
                      </div>
                    </TableCell>
                    <TableCell>{type}</TableCell>
                    <TableCell>{item.sensitivity}</TableCell>
                    <TableCell><StatusBadge status={item.status} /></TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => updateGovernanceUrl({
                          review_type: kind,
                          review_id: id,
                          source_version: kind === "source" ? (item as SourceReviewQueueItem).source_version_id : null,
                        }, "push")}
                      >
                        <SearchCheckIcon data-icon="inline-start" aria-hidden="true" /> {t("governance.action.inspect")}
                      </Button>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>
      <div className={`${styles.actionRow} mt-3`}>
        <Button variant="outline" size="sm" disabled={offset === 0 || loading} onClick={() => { setLoading(true); updateGovernanceUrl({ review_offset: Math.max(0, offset - PAGE_SIZE), review_id: null, review_type: null, source_version: null }, "push") }}>{t("governance.action.previous")}</Button>
        <span className={`${styles.mono} ${styles.muted}`}>{items.length ? `${offset + 1}–${offset + items.length}` : "0"}</span>
        <Button variant="outline" size="sm" disabled={items.length < PAGE_SIZE || loading} onClick={() => { setLoading(true); updateGovernanceUrl({ review_offset: offset + PAGE_SIZE, review_id: null, review_type: null, source_version: null }, "push") }}>{t("governance.action.next")}</Button>
      </div>

      {selected?.kind === "source" && selected.sourceVersionId && (
        <SourceReviewSheet
          key={selected.sourceVersionId}
          open
          sourceId={selected.id}
          sourceVersionId={selected.sourceVersionId}
          onOpenChange={(open) => !open && updateGovernanceUrl({ review_id: null, review_type: null, source_version: null }, "push")}
          onReviewed={reviewed}
        />
      )}
      {selected?.kind === "claim" && (
        <ClaimReviewSheet
          key={selected.id}
          open
          claimId={selected.id}
          actor={actor}
          onOpenChange={(open) => !open && updateGovernanceUrl({ review_id: null, review_type: null, source_version: null }, "push")}
          onReviewed={reviewed}
        />
      )}
      {selected?.kind === "mapping" && (
        <MappingReviewSheet
          key={selected.id}
          open
          mappingId={selected.id}
          actor={actor}
          onOpenChange={(open) => !open && updateGovernanceUrl({ review_id: null, review_type: null, source_version: null }, "push")}
          onReviewed={reviewed}
        />
      )}
    </section>
  )
}
