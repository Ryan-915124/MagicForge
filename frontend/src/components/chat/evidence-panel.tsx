"use client"

import dynamic from "next/dynamic"
import { useId, useState, type CSSProperties } from "react"
import {
  BookMarkedIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  FileSearchIcon,
  FileTextIcon,
  MicroscopeIcon,
  NetworkIcon,
  PenLineIcon,
  ShieldAlertIcon,
  type LucideIcon,
} from "lucide-react"

import { isKnowledgeOrigin } from "@/components/shared/origin-badge"
import { useLocale } from "@/components/i18n/locale-provider"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { KnowledgeOrigin, SourceSummary } from "@/lib/api/types"
import { humanize } from "@/lib/format"
import type { MessageKey, MessageValues } from "@/lib/i18n/messages"

type Translator = (key: MessageKey, values?: MessageValues) => string

const RetrievalTrace = dynamic(
  () => import("@/components/chat/retrieval-trace").then((module) => module.RetrievalTrace),
  { ssr: false }
)

const filters: Array<{ value: "all" | KnowledgeOrigin; labelKey: MessageKey }> = [
  { value: "all", labelKey: "chat.evidence.allRecords" },
  { value: "scientific_evidence", labelKey: "chat.evidence.scientific" },
  { value: "expert_practice", labelKey: "chat.evidence.practitioner" },
  { value: "personal_interpretation", labelKey: "chat.evidence.interpretation" },
]

const originDetails: Record<KnowledgeOrigin, { labelKey: MessageKey; noteKey: MessageKey; icon: LucideIcon }> = {
  scientific_evidence: {
    labelKey: "chat.evidence.scientificEvidence",
    noteKey: "chat.evidence.scientificNote",
    icon: MicroscopeIcon,
  },
  expert_practice: {
    labelKey: "chat.evidence.practitionerRecord",
    noteKey: "chat.evidence.practitionerNote",
    icon: BookMarkedIcon,
  },
  personal_interpretation: {
    labelKey: "chat.evidence.interpretation",
    noteKey: "chat.evidence.interpretationNote",
    icon: PenLineIcon,
  },
}

function externalSourceUrl(locator: string | null): string | null {
  if (!locator) return null
  try {
    const url = new URL(locator)
    if (url.protocol !== "http:" && url.protocol !== "https:") return null
    if (url.username || url.password) return null
    return url.href
  } catch {
    return null
  }
}

function SourceCard({ source, index, t }: { source: SourceSummary; index: number; t: Translator }) {
  const origin = isKnowledgeOrigin(source.knowledge_origin) ? source.knowledge_origin : null
  const details = origin
    ? originDetails[origin]
    : {
        labelKey: "chat.evidence.unclassified" as const,
        noteKey: "chat.evidence.requiresReview" as const,
        icon: FileTextIcon,
      }
  const Icon = details.icon
  const confidence =
    source.confidence == null || !Number.isFinite(source.confidence)
      ? null
      : Math.max(0, Math.min(1, source.confidence))
  const matchScore = Math.round(
    Math.max(0, Math.min(1, Number.isFinite(source.score) ? source.score : 0)) * 100
  )
  const sourceUrl = externalSourceUrl(source.source_locator)

  return (
    <article
      className="source-dossier"
      data-origin={origin ?? "unclassified"}
      aria-label={t("chat.evidence.sourceLabel", {
        number: index + 1,
        title: source.title || t("chat.evidence.untitled"),
      })}
    >
      <div className="source-dossier-tab" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
      <header className="source-dossier-header">
        <div className="source-origin-seal">
          <span><Icon aria-hidden="true" /></span>
          <div>
            <p>{t(details.labelKey)}</p>
            <small>{t(details.noteKey)}</small>
          </div>
        </div>
        <span className="retrieval-score">
          {t("chat.evidence.match", { score: matchScore })}
        </span>
      </header>

      <div className="source-dossier-body">
        <h3 className="break-words" translate="no">
          {source.title || t("chat.evidence.untitled")}
        </h3>
        <p className="source-author break-words" translate="no">
          {source.author || t("chat.evidence.authorMissing")}
        </p>

        <div className="source-evidence-tags">
          {source.evidence_class ? <span data-tone="evidence" translate="no">{humanize(source.evidence_class)}</span> : null}
          {source.magic_category ? <span data-tone="craft" translate="no">{source.magic_category}</span> : null}
          {source.confidence_label ? <span data-tone="confidence" translate="no">{humanize(source.confidence_label)}</span> : null}
        </div>

        <div className="confidence-instrument">
          <div className="confidence-instrument-label">
            <span>{t("chat.evidence.confidence")}</span>
            <strong>
              {confidence == null
                ? t("chat.evidence.confidenceNotAssessed")
                : `${Math.round(confidence * 100)}%`}
            </strong>
          </div>
          {confidence == null ? null : (
            <>
              <div
                className="confidence-ruler"
                style={{ "--confidence": `${confidence * 100}%` } as CSSProperties}
                aria-hidden="true"
              >
                <i /><i /><i /><i /><i />
              </div>
              <meter
                className="sr-only"
                min={0}
                max={1}
                value={confidence}
                aria-label={t("chat.evidence.confidence")}
              >
                {t("chat.evidence.confidence")} {Math.round(confidence * 100)}%
              </meter>
            </>
          )}
        </div>

        {source.limitations.length > 0 ? (
          <aside className="source-limitation-note">
            <ShieldAlertIcon aria-hidden="true" />
            <div>
              <p>{t("chat.evidence.marginWarning")}</p>
              <span className="break-words" translate="no">{source.limitations.join(" · ")}</span>
            </div>
          </aside>
        ) : null}
      </div>

      {(source.source_locator || source.evidence_card_id) ? (
        <footer className="source-dossier-footer">
          <span translate="no">
            {source.evidence_card_id
              ? `card:${source.evidence_card_id.slice(0, 8)}`
              : t("chat.evidence.sourceRecord")}
          </span>
          {sourceUrl ? (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noreferrer"
              aria-label={t("chat.evidence.openRecordLabel", {
                title: source.title || t("chat.evidence.untitled"),
              })}
            >
              {t("chat.evidence.openRecord")} <ExternalLinkIcon aria-hidden="true" />
            </a>
          ) : source.source_locator ? (
            <span className="source-location-label" translate="no">
              {t("chat.evidence.locator", { locator: source.source_locator })}
            </span>
          ) : null}
        </footer>
      ) : null}
    </article>
  )
}

type IndexedSource = {
  source: SourceSummary
  sourceIndex: number
}

function SourceList({ sources, t }: { sources: IndexedSource[]; t: Translator }) {
  if (sources.length === 0) {
    return (
      <div className="closed-evidence-folio">
        <span className="folio-string" aria-hidden="true" />
        <FileSearchIcon aria-hidden="true" />
        <p>{t("chat.evidence.closed")}</p>
        <span>{t("chat.evidence.closedDescription")}</span>
      </div>
    )
  }
  return (
    <div className="evidence-dossier-stack flex flex-col gap-4">
      {sources.map(({ source, sourceIndex }) => (
        <SourceCard
          key={`${source.document_id ?? source.title}-${sourceIndex}`}
          source={source}
          index={sourceIndex}
          t={t}
        />
      ))}
    </div>
  )
}

export function EvidencePanel({ sources }: { sources: SourceSummary[] }) {
  const { t } = useLocale()
  const [open, setOpen] = useState(false)
  const contentId = useId()
  const titleId = useId()
  const indexedSources = sources.map((source, sourceIndex) => ({ source, sourceIndex }))

  if (sources.length === 0) {
    return (
      <section className="evidence-folio-compact" aria-label={t("chat.evidence.awaitingLabel")}>
        <div className="evidence-folio-clasp" aria-hidden="true"><i /><i /><i /></div>
        <FileSearchIcon aria-hidden="true" />
        <div>
          <span>{t("chat.evidence.archive")}</span>
          <h2>{t("chat.evidence.awaitingTitle")}</h2>
          <p>{t("chat.evidence.awaitingDescription")}</p>
        </div>
        <div className="folio-case-number"><b>00</b><span>{t("chat.evidence.records")}</span></div>
      </section>
    )
  }

  return (
    <section
      className="evidence-folio evidence-folio-drawer"
      data-expanded={open}
      aria-labelledby={titleId}
    >
      <div className="evidence-folio-clasp" aria-hidden="true"><i /><i /><i /></div>
      <div className="evidence-drawer-summary">
        <div className="evidence-folio-header">
          <div>
            <span>{t("chat.evidence.archive")}</span>
            <h2 id={titleId}>{open ? t("chat.evidence.opened") : t("chat.evidence.examine")}</h2>
            <p>{t("chat.evidence.summary", { count: sources.length })}</p>
          </div>
          <div className="folio-case-number"><b>{String(sources.length).padStart(2, "0")}</b><span>{t("chat.evidence.records")}</span></div>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="lg"
          className="evidence-drawer-action"
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen((current) => !current)}
        >
          {open ? t("chat.evidence.close") : t("chat.evidence.open")}
          <ChevronDownIcon data-icon="inline-end" aria-hidden="true" />
        </Button>
      </div>

      <div id={contentId} className="evidence-drawer-content" hidden={!open}>
        {open ? (
          <Tabs defaultValue="all" className="flex min-h-0 flex-col">
              <div className="evidence-layer-tab-rail">
                <TabsList className="evidence-layer-tabs">
                  {filters.map((filter) => (
                    <TabsTrigger key={filter.value} value={filter.value} data-origin={filter.value}>
                      {t(filter.labelKey)}
                      <small>{filter.value === "all" ? sources.length : sources.filter((source) => source.knowledge_origin === filter.value).length}</small>
                    </TabsTrigger>
                  ))}
                </TabsList>
              </div>
              {filters.map((filter) => {
                const filtered =
                  filter.value === "all"
                    ? indexedSources
                    : indexedSources.filter(
                        ({ source }) =>
                          isKnowledgeOrigin(source.knowledge_origin) && source.knowledge_origin === filter.value
                      )
                return (
                  <TabsContent key={filter.value} value={filter.value} className="min-h-0">
                    <div className="evidence-dossier-stage px-5 py-6 lg:px-8 lg:py-8">
                      <SourceList sources={filtered} t={t} />
                    </div>
                  </TabsContent>
                )
              })}
            </Tabs>
        ) : null}
      </div>
    </section>
  )
}

export function RelatedKnowledgePaths({ question, sources }: { question: string; sources: SourceSummary[] }) {
  const { t } = useLocale()
  const [open, setOpen] = useState(false)
  const contentId = useId()
  const titleId = useId()

  if (sources.length === 0) return null

  return (
    <section className="knowledge-paths-drawer" data-expanded={open} aria-labelledby={titleId}>
      <div className="knowledge-paths-summary">
        <span className="knowledge-paths-icon"><NetworkIcon aria-hidden="true" /></span>
        <span>
          <small>{t("chat.evidence.appendix")}</small>
          <h2 id={titleId}><strong>{t("chat.evidence.traceTitle")}</strong></h2>
          <em>{t("chat.evidence.threadCount", { count: Math.min(sources.length, 4) })}</em>
        </span>
        <Button
          type="button"
          variant="ghost"
          size="lg"
          className="knowledge-paths-action"
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen((current) => !current)}
        >
          {open ? t("chat.evidence.closeAppendix") : t("chat.evidence.openAppendix")}
          <ChevronDownIcon data-icon="inline-end" aria-hidden="true" />
        </Button>
      </div>
      <div id={contentId} className="knowledge-paths-content" hidden={!open}>
        {open ? (
          <>
            <p className="evidence-thread-note">
              {t("chat.evidence.retrievalDisclaimer")}
            </p>
            <RetrievalTrace question={question} sources={sources} />
          </>
        ) : null}
      </div>
    </section>
  )
}
