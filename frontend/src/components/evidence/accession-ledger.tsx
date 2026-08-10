"use client"

import { memo, type MouseEvent } from "react"
import { FileSearchIcon } from "lucide-react"

import { clampUnitScore, originPresentation, shortEvidenceId } from "@/components/evidence/evidence-display"
import { useLocale } from "@/components/i18n/locale-provider"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import type { EvidenceCard } from "@/lib/api/types"
import { formatPercent, humanize } from "@/lib/format"

interface AccessionLedgerProps {
  cards: EvidenceCard[]
  selectedId: string | null
  loading: boolean
  hasRetrieved: boolean
  hrefFor: (id: string) => string
  onSelect: (id: string) => void
}

function AccessionLedgerComponent({
  cards,
  selectedId,
  loading,
  hasRetrieved,
  hrefFor,
  onSelect,
}: AccessionLedgerProps) {
  const { locale, t } = useLocale()

  return (
    <section className="evidence-accession-ledger" aria-labelledby="accession-ledger-title" aria-busy={loading}>
      <div className="evidence-ledger-heading">
        <div>
          <span>{t("evidence.ledger.accession")}</span>
          <h2 id="accession-ledger-title">{t("evidence.ledger.title")}</h2>
        </div>
        <strong>{cards.length.toString().padStart(2, "0")}</strong>
      </div>

      <div className="evidence-ledger-rail" aria-hidden="true" />

      {cards.length > 0 ? (
        <ScrollArea
          className="evidence-ledger-scroll"
          tabIndex={0}
          aria-label={t("evidence.ledger.fileCount", { count: cards.length })}
        >
          <ol className="evidence-ledger-list">
            {cards.map((card, index) => {
              const origin = originPresentation[card.knowledge_origin]
              const assessment = card.confidence?.score
              const score = assessment ?? card.extraction_confidence
              const scoreKind = assessment === undefined
                ? t("evidence.ledger.extraction")
                : t("evidence.ledger.evidence")
              const isSelected = card.id === selectedId

              function select(event: MouseEvent<HTMLAnchorElement>) {
                if (
                  event.defaultPrevented ||
                  event.button !== 0 ||
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                ) return
                event.preventDefault()
                onSelect(card.id)
              }

              return (
                <li key={card.id}>
                  <a
                    href={hrefFor(card.id)}
                    className="evidence-ledger-entry"
                    data-origin={card.knowledge_origin}
                    data-selected={isSelected}
                    aria-current={isSelected ? "location" : undefined}
                    onClick={select}
                  >
                    <span className="evidence-ledger-position" aria-hidden="true">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span className="evidence-ledger-entry-body">
                      <span className="evidence-ledger-entry-meta">
                        <strong>{origin.code}</strong>
                        <span translate="no">{shortEvidenceId(card.id)}</span>
                        <span translate="no">{humanize(card.evidence_class)}</span>
                      </span>
                      <span className="evidence-ledger-claim" translate="no">{card.claim}</span>
                      <span className="evidence-ledger-entry-footer">
                        <span translate="no">{origin.label}</span>
                        <span>{scoreKind} {formatPercent(clampUnitScore(score), locale)}</span>
                      </span>
                    </span>
                  </a>
                </li>
              )
            })}
          </ol>
        </ScrollArea>
      ) : loading ? (
        <div className="evidence-ledger-loading" aria-label={t("evidence.ledger.retrieving")}>
          {[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-28 w-full rounded-sm" />)}
        </div>
      ) : (
        <Empty className="evidence-ledger-empty">
          <EmptyHeader>
            <EmptyMedia variant="icon"><FileSearchIcon aria-hidden="true" /></EmptyMedia>
            <EmptyTitle>
              {hasRetrieved ? t("evidence.ledger.noMatch") : t("evidence.ledger.closed")}
            </EmptyTitle>
            <EmptyDescription>
              {hasRetrieved
                ? t("evidence.ledger.adjust")
                : t("evidence.ledger.retrieveDescription")}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </section>
  )
}

export const AccessionLedger = memo(AccessionLedgerComponent)
