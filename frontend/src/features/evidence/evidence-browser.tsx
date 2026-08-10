"use client"

import Link from "next/link"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowRightIcon,
  ArchiveXIcon,
  FingerprintIcon,
  LockKeyholeIcon,
  RotateCwIcon,
  ScanSearchIcon,
  ShieldCheckIcon,
} from "lucide-react"

import { AccessionLedger } from "@/components/evidence/accession-ledger"
import {
  ArchiveCatalogue,
  DEFAULT_ARCHIVE_FILTERS,
  type ArchiveFilters,
} from "@/components/evidence/archive-catalogue"
import { EvidenceArchiveHeader } from "@/components/evidence/archive-header"
import { EvidenceDossier } from "@/components/evidence/evidence-card-view"
import { MotionPage } from "@/components/app-shell/motion-page"
import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { MagicForgeApiError, magicForgeApi } from "@/lib/api/client"
import type {
  CorpusStatsResponse,
  EvidenceCard,
  GraphProjectionSummary,
  KnowledgeOrigin,
  MagicDomain,
} from "@/lib/api/types"

type ArchiveStatus = "idle" | "loading" | "ready" | "empty" | "sealed" | "error"

const sealedCorpusCodes = new Set([
  "active_corpus_not_configured",
  "active_corpus_not_authorized",
])

const origins = new Set<KnowledgeOrigin>([
  "scientific_evidence",
  "expert_practice",
  "personal_interpretation",
])
const domains = new Set<MagicDomain>(["card", "close-up", "stage", "mentalism", "theory"])
const levels = new Set<ArchiveFilters["level"]>(["all", "empirical", "review", "practitioner", "anecdotal"])

function archiveHref(filters: ArchiveFilters, dossierId?: string | null) {
  const params = new URLSearchParams()
  if (filters.query) params.set("q", filters.query)
  if (filters.origin !== "all") params.set("origin", filters.origin)
  if (filters.domain !== "all") params.set("domain", filters.domain)
  if (filters.level !== "all") params.set("level", filters.level)
  if (params.size === 0) params.set("browse", "1")
  if (dossierId) params.set("evidence", dossierId)
  return `/evidence?${params.toString()}`
}

function readArchiveLocation() {
  const params = new URLSearchParams(window.location.search)
  const rawOrigin = params.get("origin")
  const rawDomain = params.get("domain")
  const rawLevel = params.get("level")

  return {
    filters: {
      query: params.get("q")?.trim() ?? "",
      origin: rawOrigin && origins.has(rawOrigin as KnowledgeOrigin)
        ? rawOrigin as ArchiveFilters["origin"]
        : "all",
      domain: rawDomain && domains.has(rawDomain as MagicDomain)
        ? rawDomain as ArchiveFilters["domain"]
        : "all",
      level: rawLevel && levels.has(rawLevel as ArchiveFilters["level"])
        ? rawLevel as ArchiveFilters["level"]
        : "all",
    } satisfies ArchiveFilters,
    dossierId: params.get("evidence"),
  }
}

function replaceArchiveLocation(filters: ArchiveFilters, dossierId?: string | null) {
  window.history.replaceState({ magicforgeArchive: true }, "", archiveHref(filters, dossierId))
}

function pushArchiveLocation(filters: ArchiveFilters, dossierId?: string | null) {
  window.history.pushState({ magicforgeArchive: true }, "", archiveHref(filters, dossierId))
}

function isMobileArchive() {
  return window.matchMedia("(max-width: 767px)").matches
}

export function EvidenceBrowser() {
  const { locale, t } = useLocale()
  const [filters, setFilters] = useState<ArchiveFilters>(DEFAULT_ARCHIVE_FILTERS)
  const [cards, setCards] = useState<EvidenceCard[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [status, setStatus] = useState<ArchiveStatus>("idle")
  const [error, setError] = useState<MagicForgeApiError | null>(null)
  const [projection, setProjection] = useState<GraphProjectionSummary | null>(null)
  const [stats, setStats] = useState<CorpusStatsResponse | null>(null)
  const [announcement, setAnnouncement] = useState("")
  const [mobileDossierOpen, setMobileDossierOpen] = useState(false)
  const requestSequence = useRef(0)
  const translateRef = useRef(t)

  useEffect(() => {
    translateRef.current = t
  }, [t])

  const retrieve = useCallback(async (
    nextFilters: ArchiveFilters,
    options: {
      updateHistory?: boolean
      preferredId?: string | null
      openMobile?: boolean
    } = {}
  ) => {
    const requestId = ++requestSequence.current
    const normalized: ArchiveFilters = { ...nextFilters, query: nextFilters.query.trim() }
    setFilters(normalized)
    setStatus("loading")
    setError(null)
    if (options.updateHistory) pushArchiveLocation(normalized)

    try {
      const response = await magicForgeApi.search({
        query: normalized.query,
        limit: 20,
        knowledge_types: ["evidence"],
        knowledge_origins: normalized.origin === "all" ? [] : [normalized.origin],
        domains: normalized.domain === "all" ? [] : [normalized.domain],
        evidence_levels: normalized.level === "all" ? [] : [normalized.level],
      })
      if (requestId !== requestSequence.current) return

      let nextCards = response.evidence_cards ?? []
      const preferredId = options.preferredId
      if (preferredId && !nextCards.some((card) => card.id === preferredId)) {
        try {
          const directCard = await magicForgeApi.evidence(preferredId)
          if (requestId !== requestSequence.current) return
          nextCards = [directCard, ...nextCards.filter((card) => card.id !== directCard.id)].slice(0, 20)
        } catch {
          // A deep-linked dossier may be unavailable under current security filters.
        }
      }

      setCards(nextCards)
      setProjection(response.projection)
      const nextSelected =
        (preferredId && nextCards.some((card) => card.id === preferredId) ? preferredId : null) ??
        nextCards[0]?.id ??
        null
      setSelectedId(nextSelected)
      setStatus(nextCards.length > 0 ? "ready" : "empty")
      setAnnouncement(
        nextCards.length === 1
          ? translateRef.current("evidence.browser.retrievedSingle")
          : translateRef.current("evidence.browser.retrievedPlural", { count: nextCards.length })
      )
      if (options.openMobile && nextSelected && isMobileArchive()) setMobileDossierOpen(true)
    } catch (cause) {
      if (requestId !== requestSequence.current) return
      const apiError = cause instanceof MagicForgeApiError
        ? cause
        : new MagicForgeApiError(
          translateRef.current("evidence.browser.fallbackError"),
          "backend_error",
          500
        )
      const sealed = sealedCorpusCodes.has(apiError.code)
      setStatus(sealed ? "sealed" : "error")
      setError(apiError)
      if (sealed) {
        setCards([])
        setSelectedId(null)
        setProjection(null)
      }
      setAnnouncement(translateRef.current(sealed ? "evidence.browser.sealedAnnouncement" : "evidence.browser.unreachable"))
    }
  }, [])

  useEffect(() => {
    let mounted = true
    magicForgeApi.stats().then((response) => {
      if (mounted) setStats(response)
    }).catch(() => {
      // Production stays uncounted when the governed read model is unavailable.
    })

    const initial = readArchiveLocation()
    const initialRequest = window.setTimeout(() => {
      void retrieve(initial.filters, {
        preferredId: initial.dossierId,
        openMobile: Boolean(initial.dossierId),
      })
    }, 0)

    function handleHistoryNavigation() {
      const location = readArchiveLocation()
      setMobileDossierOpen(false)
      void retrieve(location.filters, {
        preferredId: location.dossierId,
        openMobile: Boolean(location.dossierId),
      })
    }

    function handleViewportChange(event: MediaQueryListEvent) {
      if (!event.matches) setMobileDossierOpen(false)
    }

    const mobileQuery = window.matchMedia("(max-width: 767px)")
    window.addEventListener("popstate", handleHistoryNavigation)
    mobileQuery.addEventListener("change", handleViewportChange)

    return () => {
      mounted = false
      window.clearTimeout(initialRequest)
      requestSequence.current += 1
      window.removeEventListener("popstate", handleHistoryNavigation)
      mobileQuery.removeEventListener("change", handleViewportChange)
    }
  }, [retrieve])

  const selectedCard = useMemo(
    () => cards.find((card) => card.id === selectedId) ?? null,
    [cards, selectedId]
  )

  const hrefFor = useCallback((id: string) => archiveHref(filters, id), [filters])

  function selectDossier(id: string) {
    setSelectedId(id)
    pushArchiveLocation(filters, id)
    setAnnouncement(t("evidence.browser.openedAnnouncement"))
    if (isMobileArchive()) setMobileDossierOpen(true)
  }

  function changeMobileDossier(open: boolean) {
    setMobileDossierOpen(open)
    if (!open) replaceArchiveLocation(filters)
  }

  const loading = status === "loading"
  const archiveSealed = status === "sealed"
  const preservedCount = projection?.evidence_cards ?? stats?.evidence_cards

  return (
    <MotionPage className="evidence-archive-page">
      <EvidenceArchiveHeader stats={stats} projection={projection} sealed={archiveSealed} />

      <div className="evidence-archive-workspace">
        <aside className="evidence-catalogue-cabinet" aria-label={t("evidence.browser.cabinetLabel")}>
          <ArchiveCatalogue
            key={`${filters.query}:${filters.origin}:${filters.domain}:${filters.level}`}
            appliedFilters={filters}
            pending={loading}
            disabled={archiveSealed}
            onRetrieve={(nextFilters) => void retrieve(nextFilters, { updateHistory: true })}
          />
          {archiveSealed ? (
            <div className="evidence-sealed-ledger" aria-hidden="true">
              <LockKeyholeIcon />
              <span>{t("evidence.browser.registerSealed")}</span>
            </div>
          ) : (
            <AccessionLedger
              cards={cards}
              selectedId={selectedId}
              loading={loading}
              hasRetrieved={status !== "idle" && status !== "loading"}
              hrefFor={hrefFor}
              onSelect={selectDossier}
            />
          )}
        </aside>

        <section
          className="evidence-inspection-desk"
          aria-labelledby="inspection-desk-title"
          aria-busy={loading}
          data-sealed={archiveSealed}
        >
          <header className="evidence-inspection-heading">
            <div>
              <span>{t("evidence.browser.readingTable")}</span>
              <h2 id="inspection-desk-title">{t("evidence.browser.inspection")}</h2>
            </div>
            <p>
              {t("evidence.browser.shown", { count: cards.length.toLocaleString(locale) })}
              {preservedCount !== undefined
                ? ` · ${t("evidence.browser.preserved", { count: preservedCount.toLocaleString(locale) })}`
                : ""}
            </p>
          </header>

          {error && !archiveSealed && (
            <Alert className="evidence-archive-error">
              <ArchiveXIcon aria-hidden="true" />
              <AlertTitle>{t("evidence.browser.errorTitle")}</AlertTitle>
              <AlertDescription>
                <p translate="no">{error.message}</p>
                <Button
                  type="button"
                  variant="outline"
                  className="evidence-retry-button"
                  onClick={() => void retrieve(filters, { preferredId: selectedId })}
                >
                  <RotateCwIcon data-icon="inline-start" aria-hidden="true" />
                  {t("evidence.browser.retry")}
                </Button>
              </AlertDescription>
            </Alert>
          )}

          <div className="evidence-desktop-dossier">
            {archiveSealed ? (
              <div className="evidence-production-sealed" aria-labelledby="production-sealed-title">
                <div className="evidence-production-seal-mark" aria-hidden="true">
                  <FingerprintIcon />
                  <LockKeyholeIcon />
                </div>
                <span>{t("evidence.browser.productionSeal")}</span>
                <h3 id="production-sealed-title">{t("evidence.browser.sealedTitle")}</h3>
                <p>{t("evidence.browser.sealedDescription")}</p>
                <ol aria-label={t("evidence.browser.unlockSequence")}>
                  <li><ShieldCheckIcon aria-hidden="true" /><span><strong>{t("evidence.browser.unlockReview")}</strong>{t("evidence.browser.unlockReviewDescription")}</span></li>
                  <li><ScanSearchIcon aria-hidden="true" /><span><strong>{t("evidence.browser.unlockManifest")}</strong>{t("evidence.browser.unlockManifestDescription")}</span></li>
                  <li><FingerprintIcon aria-hidden="true" /><span><strong>{t("evidence.browser.unlockActivation")}</strong>{t("evidence.browser.unlockActivationDescription")}</span></li>
                </ol>
                <Button render={<Link href="/governance?desk=review" />} nativeButton={false}>
                  {t("evidence.browser.openGovernance")}
                  <ArrowRightIcon data-icon="inline-end" aria-hidden="true" />
                </Button>
              </div>
            ) : selectedCard ? (
              <EvidenceDossier key={selectedCard.id} card={selectedCard} />
            ) : loading ? (
              <div className="evidence-inspection-loading" aria-label={t("evidence.browser.preparing")}>
                <Skeleton className="h-28 w-full rounded-sm" />
                <Skeleton className="h-[34rem] w-full rounded-sm" />
              </div>
            ) : (
              <div className="evidence-inspection-empty">
                <ScanSearchIcon aria-hidden="true" />
                <span>{t("evidence.browser.unassigned")}</span>
                <h3>
                  {status === "empty"
                    ? t("evidence.browser.noCompatible")
                    : t("evidence.browser.select")}
                </h3>
                <p>{t("evidence.browser.emptyDescription")}</p>
              </div>
            )}
          </div>
        </section>
      </div>

      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>

      <Sheet open={mobileDossierOpen && Boolean(selectedCard)} onOpenChange={changeMobileDossier}>
        <SheetContent side="bottom" className="evidence-mobile-dossier-sheet" showCloseButton>
          <SheetHeader className="evidence-mobile-dossier-header">
            <SheetTitle>{t("evidence.browser.sheetTitle")}</SheetTitle>
            <SheetDescription>{t("evidence.browser.sheetDescription")}</SheetDescription>
          </SheetHeader>
          <ScrollArea className="evidence-mobile-dossier-scroll">
            {selectedCard && <EvidenceDossier card={selectedCard} surface="sheet" />}
          </ScrollArea>
        </SheetContent>
      </Sheet>
    </MotionPage>
  )
}
