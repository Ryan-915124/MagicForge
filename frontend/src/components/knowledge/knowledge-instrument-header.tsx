"use client"

import { ApertureIcon, CircleGaugeIcon, Layers3Icon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Badge } from "@/components/ui/badge"
import type { GraphProjectionSummary } from "@/lib/api/types"

import styles from "./knowledge-explorer.module.css"

export function KnowledgeInstrumentHeader({
  projection,
  visibleNodes,
  visibleRelationships,
}: {
  projection: GraphProjectionSummary | null
  visibleNodes: number
  visibleRelationships: number
}) {
  const { t } = useLocale()

  return (
    <header className={styles.instrumentHeader}>
      <div className={styles.headerIdentity}>
        <span className={styles.headerSigil} aria-hidden="true">
          <ApertureIcon />
        </span>
        <div>
          <div className={styles.headerKicker}>
            <span translate="no">MagicForge / Constellation 03</span>
            <Badge variant="outline">{t("knowledge.header.bootstrap")}</Badge>
          </div>
          <h1 translate="no">The Impossible Orrery</h1>
          <p>{t("knowledge.header.description")}</p>
        </div>
      </div>

      <div className={styles.headerReadout} aria-label={t("knowledge.header.status")}>
        <div>
          <CircleGaugeIcon aria-hidden="true" />
          <span>
            <small>{t("knowledge.header.projection")}</small>
            <strong>{projection?.run_id ?? t("knowledge.header.calibrating")}</strong>
          </span>
        </div>
        <div>
          <Layers3Icon aria-hidden="true" />
          <span>
            <small>{t("knowledge.header.knownUniverse")}</small>
            <strong>{t("knowledge.header.universeCount", { nodes: visibleNodes, relationships: visibleRelationships })}</strong>
          </span>
        </div>
        <code>{projection?.collection ?? t("knowledge.header.manifestPending")}</code>
      </div>
    </header>
  )
}
