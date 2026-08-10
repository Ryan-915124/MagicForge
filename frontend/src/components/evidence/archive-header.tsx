"use client"

import { ArchiveIcon, FingerprintIcon, ShieldCheckIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import type { CorpusStatsResponse, GraphProjectionSummary } from "@/lib/api/types"

interface ArchiveHeaderProps {
  stats: CorpusStatsResponse | null
  projection: GraphProjectionSummary | null
  sealed?: boolean
}

export function EvidenceArchiveHeader({ stats, projection, sealed = false }: ArchiveHeaderProps) {
  const { locale, t } = useLocale()
  const sources = stats?.sources ?? projection?.sources
  const evidenceCards =
    stats?.evidence_cards ?? projection?.evidence_cards
  const humanVerified = stats?.human_verified ?? (projection?.human_verified ? evidenceCards : undefined)
  const collection = stats?.collection ?? projection?.collection
  const collectionLabel = sealed
    ? t("evidence.header.awaitingCorpus")
    : collection?.replace(/^magicforge_/, "") ?? "—"
  const mode = sealed
    ? "PRODUCTION / SEALED"
    : stats?.mode ?? (projection?.bootstrap_generated ? "bootstrap" : "—")

  return (
    <header className="evidence-archive-header" aria-labelledby="evidence-archive-title">
      <div className="evidence-archive-accession-rail" aria-hidden="true">
        <span>01</span><span>04</span><span>08</span><span>12</span>
      </div>

      <div className="evidence-archive-heading">
        <p className="evidence-archive-kicker">
          <ArchiveIcon aria-hidden="true" /> {t("evidence.header.privateRegistry")}
        </p>
        <h1 id="evidence-archive-title">{t("evidence.header.title")}</h1>
        <p>{t("evidence.header.description")}</p>
      </div>

      <dl className="evidence-archive-register" aria-label={t("evidence.header.statusLabel")}>
        <div>
          <dt>{t("evidence.header.collection")}</dt>
          <dd translate={sealed ? undefined : "no"} aria-label={collection}>{collectionLabel}</dd>
        </div>
        <div>
          <dt>{t("evidence.header.sourcesPreserved")}</dt>
          <dd>{sources === undefined ? "—" : sources.toLocaleString(locale)}</dd>
        </div>
        <div>
          <dt>{t("evidence.header.files")}</dt>
          <dd>{evidenceCards === undefined ? "—" : evidenceCards.toLocaleString(locale)}</dd>
        </div>
      </dl>

      <div className="evidence-archive-seal" data-verified={(humanVerified ?? 0) > 0} data-sealed={sealed}>
        <FingerprintIcon aria-hidden="true" />
        <span translate="no">{mode}</span>
        <strong>
          {sealed
            ? t("evidence.header.activationRequired")
            : humanVerified === undefined
              ? t("evidence.header.verificationUnavailable")
              : t("evidence.header.humanVerified", { count: humanVerified.toLocaleString(locale) })}
        </strong>
        <ShieldCheckIcon aria-hidden="true" />
      </div>
    </header>
  )
}
