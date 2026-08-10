"use client"

import Link from "next/link"
import { memo, useState, type CSSProperties } from "react"
import { ScanLineIcon, TriangleIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { CorpusObservation } from "@/features/dashboard/corpus-observation"
import { formatCount, humanize } from "@/lib/format"
import type { MessageKey } from "@/lib/i18n/messages"

import styles from "./corpus-observatory.module.css"

const domainOrder = ["theory", "stage", "close-up", "mentalism", "card"]
const originOrder = [
  "scientific_evidence",
  "expert_practice",
  "personal_interpretation",
]

const domainDescriptionKeys: Record<string, MessageKey> = {
  theory: "dashboard.survey.domainTheory",
  stage: "dashboard.survey.domainStage",
  "close-up": "dashboard.survey.domainCloseUp",
  mentalism: "dashboard.survey.domainMentalism",
  card: "dashboard.survey.domainCard",
}

const originMetadata = {
  scientific_evidence: {
    label: "Scientific evidence",
    shortLabel: "Science",
    descriptionKey: "dashboard.prism.scienceDescription",
  },
  expert_practice: {
    label: "Expert practice",
    shortLabel: "Practice",
    descriptionKey: "dashboard.prism.practiceDescription",
  },
  personal_interpretation: {
    label: "Interpretation",
    shortLabel: "Interpretation",
    descriptionKey: "dashboard.prism.interpretationDescription",
  },
} as const satisfies Record<string, {
  label: string
  shortLabel: string
  descriptionKey: MessageKey
}>

interface CorpusInstrumentsProps {
  observation: CorpusObservation
}

function orderedDomains(domains: Record<string, number>) {
  return Object.entries(domains).sort(([left], [right]) => {
    const leftIndex = domainOrder.indexOf(left)
    const rightIndex = domainOrder.indexOf(right)
    if (leftIndex === -1 && rightIndex === -1) return left.localeCompare(right)
    if (leftIndex === -1) return 1
    if (rightIndex === -1) return -1
    return leftIndex - rightIndex
  })
}

function DomainSurveyPlate({ observation }: CorpusInstrumentsProps) {
  const { locale, t } = useLocale()
  const domains = orderedDomains(observation.domains)
  const [selectedDomain, setSelectedDomain] = useState(domains[0]?.[0] ?? "")
  const activeDomain = domains.find(([domain]) => domain === selectedDomain) ?? domains[0]
  const maximum = Math.max(...domains.map(([, value]) => value), 1)
  const memberships = domains.reduce((total, [, value]) => total + value, 0)
  const activeValue = activeDomain?.[1] ?? 0
  const activeKey = activeDomain?.[0] ?? ""
  const coverage = observation.counts.fragments
    ? Math.round((activeValue / observation.counts.fragments) * 100)
    : 0

  return (
    <section className={styles.surveyPlate} aria-labelledby="domain-survey-title">
      <div className={styles.instrumentHeading}>
        <span className={styles.instrumentNumber}>{t("dashboard.survey.instrument")}</span>
        <div>
          <p><ScanLineIcon aria-hidden="true" /> {t("dashboard.survey.name")}</p>
          <h2 id="domain-survey-title">{t("dashboard.survey.title")}</h2>
        </div>
      </div>

      <ToggleGroup
        value={activeKey ? [activeKey] : []}
        onValueChange={(value) => {
          if (value[0]) setSelectedDomain(value[0])
        }}
        variant="outline"
        size="lg"
        spacing={8}
        className={styles.apertureArray}
        aria-label={t("dashboard.survey.inspectCoverage")}
      >
        {domains.map(([domain, value]) => {
          const scale = Math.max(0.2, value / maximum)
          return (
            <ToggleGroupItem
              key={domain}
              value={domain}
              className={styles.apertureControl}
              aria-label={t("dashboard.survey.domainReading", {
                domain: humanize(domain),
                count: formatCount(value, locale),
              })}
            >
              <span
                className={styles.apertureHousing}
                style={{ "--aperture-scale": scale } as CSSProperties}
                aria-hidden="true"
              >
                <span className={styles.apertureGlass} />
                <span className={styles.apertureCrosshair} />
              </span>
              <span className={styles.apertureLabel}>{humanize(domain)}</span>
              <strong>{formatCount(value, locale)}</strong>
            </ToggleGroupItem>
          )
        })}
      </ToggleGroup>

      {activeDomain && (
        <div className={styles.surveyReading} aria-live="polite">
          <div className={styles.surveyCoordinate} aria-hidden="true">
            <span>{String(domains.findIndex(([domain]) => domain === activeKey) + 1).padStart(2, "0")}</span>
            <i />
          </div>
          <div>
            <span className={styles.readingLabel}>{t("dashboard.survey.focusedField")}</span>
            <h3>{humanize(activeKey)}</h3>
            <p>
              {t(domainDescriptionKeys[activeKey] ?? "dashboard.survey.domainFallback")}
            </p>
          </div>
          <dl>
            <div>
              <dt>{t("dashboard.survey.memberships")}</dt>
              <dd>{formatCount(activeValue, locale)}</dd>
            </div>
            <div>
              <dt>{t("dashboard.survey.projectedCoverage")}</dt>
              <dd>{coverage}%</dd>
            </div>
          </dl>
          <Link href={`/evidence?domain=${encodeURIComponent(activeKey)}`}>
            {t("dashboard.survey.inspectField")} <span aria-hidden="true">↗</span>
          </Link>
        </div>
      )}

      <p className={styles.scopeNote}>
        {t("dashboard.survey.scope", {
          memberships: formatCount(memberships, locale),
          fragments: formatCount(observation.counts.fragments, locale),
        })}
      </p>
    </section>
  )
}

function EpistemicPrism({ observation }: CorpusInstrumentsProps) {
  const { locale, t } = useLocale()
  const origins = Object.entries(observation.origins).sort(
    ([left], [right]) => originOrder.indexOf(left) - originOrder.indexOf(right)
  )
  const originTotal = origins.reduce((total, [, value]) => total + value, 0)

  return (
    <section className={styles.prismInstrument} aria-labelledby="prism-title">
      <div className={styles.instrumentHeading}>
        <span className={styles.instrumentNumber}>{t("dashboard.prism.instrument")}</span>
        <div>
          <p><TriangleIcon aria-hidden="true" /> {t("dashboard.prism.name")}</p>
          <h2 id="prism-title">{t("dashboard.prism.title")}</h2>
        </div>
      </div>

      <div className={styles.prismBench}>
        <div className={styles.incidentBeam}>
          <span>{formatCount(observation.counts.fragments, locale)}</span>
          <small>{t("dashboard.prism.fragments")}</small>
        </div>
        <div className={styles.prismObject} aria-hidden="true">
          <span />
        </div>
        <div className={styles.refractedBeams}>
          {origins.map(([origin, value]) => {
            const metadata = originMetadata[origin as keyof typeof originMetadata]
            const percentage = originTotal ? Math.round((value / originTotal) * 100) : 0
            return (
              <Link
                key={origin}
                href={`/evidence?origin=${encodeURIComponent(origin)}`}
                className={styles.originBeam}
                data-origin={origin}
                aria-label={t("dashboard.prism.originReading", {
                  origin: metadata?.label ?? humanize(origin),
                  count: formatCount(value, locale),
                  percentage,
                })}
              >
                <span className={styles.beamLine} aria-hidden="true" />
                <span>
                  <small>{metadata?.shortLabel ?? humanize(origin)}</small>
                  <strong>{formatCount(value, locale)}</strong>
                  <i>{percentage}%</i>
                </span>
              </Link>
            )
          })}
        </div>
      </div>

      <div className={styles.prismLegend}>
        {origins.map(([origin]) => {
          const metadata = originMetadata[origin as keyof typeof originMetadata]
          return (
            <p key={origin} data-origin={origin}>
              <span aria-hidden="true" />
              <span className={styles.originLegendCopy}>
                <strong>{metadata?.label ?? humanize(origin)}</strong>
                <span>
                  {metadata
                    ? t(metadata.descriptionKey)
                    : t("dashboard.prism.originFallback")}
                </span>
              </span>
            </p>
          )
        })}
      </div>
    </section>
  )
}

function CorpusInstrumentsComponent({ observation }: CorpusInstrumentsProps) {
  return (
    <div className={styles.instrumentGrid}>
      <DomainSurveyPlate observation={observation} />
      <EpistemicPrism observation={observation} />
    </div>
  )
}

export const CorpusInstruments = memo(CorpusInstrumentsComponent)
