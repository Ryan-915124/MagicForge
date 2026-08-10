"use client"

import Link from "next/link"
import {
  useEffect,
  useState,
  type ComponentType,
} from "react"
import {
  ApertureIcon,
  BookOpenCheckIcon,
  BoxesIcon,
  DatabaseIcon,
  NetworkIcon,
  RadioTowerIcon,
  RotateCwIcon,
  ScaleIcon,
  ShieldAlertIcon,
  TelescopeIcon,
  WaypointsIcon,
} from "lucide-react"

import { MotionPage } from "@/components/app-shell/motion-page"
import { CorpusInstruments } from "@/components/dashboard/corpus-instruments"
import styles from "@/components/dashboard/corpus-observatory.module.css"
import { useLocale } from "@/components/i18n/locale-provider"
import { EndpointNotice } from "@/components/shared/endpoint-notice"
import { Button } from "@/components/ui/button"
import {
  observationFromStats,
  type CorpusObservation,
} from "@/features/dashboard/corpus-observation"
import { MagicForgeApiError, magicForgeApi } from "@/lib/api/client"
import type { CorpusStatsResponse } from "@/lib/api/types"
import { formatCount, formatDate } from "@/lib/format"
import type { Locale } from "@/lib/i18n/config"
import type { MessageKey, MessageValues } from "@/lib/i18n/messages"

type MeridianStageId = "sources" | "evidence" | "concepts" | "fragments"

interface MeridianStage {
  id: MeridianStageId
  index: string
  action: string
  label: string
  value: number
  detail: string
  footnote: string
  href: string
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>
}

interface ObservationRegisterProps {
  observation: CorpusObservation
  status: "live" | "calibrating" | "unavailable"
  error: MagicForgeApiError | null
  onRetry: () => void
}

type Translator = (key: MessageKey, values?: MessageValues) => string

function buildMeridianStages(
  observation: CorpusObservation,
  t: Translator,
  locale: Locale
): MeridianStage[] {
  const academicSources = observation.sourceCategories.academic ?? 0
  const practitionerSources = observation.sourceCategories.practitioner ?? 0

  return [
    {
      id: "sources",
      index: "01",
      action: t("dashboard.engine.acquire"),
      label: t("dashboard.engine.processedOrigins"),
      value: observation.counts.sources,
      detail: t("dashboard.engine.sourcesDetail", {
        academic: formatCount(academicSources, locale),
        practitioner: formatCount(practitionerSources, locale),
      }),
      footnote: t("dashboard.engine.sourcesFootnote", {
        count: formatCount(observation.counts.sourcesWithProjectedKnowledge, locale),
      }),
      href: "/research",
      icon: BookOpenCheckIcon,
    },
    {
      id: "evidence",
      index: "02",
      action: t("dashboard.engine.trace"),
      label: t("dashboard.engine.evidencePaths"),
      value: observation.counts.evidence,
      detail: t("dashboard.engine.evidenceDetail"),
      footnote: t("dashboard.engine.evidenceFootnote", {
        count: formatCount(observation.governance.contradictionChecksPending, locale),
      }),
      href: "/evidence",
      icon: WaypointsIcon,
    },
    {
      id: "concepts",
      index: "03",
      action: t("dashboard.engine.connect"),
      label: t("dashboard.engine.connectedConcepts"),
      value: observation.counts.concepts,
      detail: t("dashboard.engine.conceptsDetail", {
        relationships: formatCount(observation.counts.relationships, locale),
        renderable: formatCount(observation.counts.renderableRelationships, locale),
      }),
      footnote: t("dashboard.engine.conceptsFootnote"),
      href: "/knowledge",
      icon: NetworkIcon,
    },
    {
      id: "fragments",
      index: "04",
      action: t("dashboard.engine.project"),
      label: t("dashboard.engine.indexedFragments"),
      value: observation.counts.fragments,
      detail: t("dashboard.engine.fragmentsDetail"),
      footnote: t("dashboard.engine.fragmentsFootnote", {
        count: formatCount(observation.counts.humanVerified, locale),
      }),
      href: "/knowledge",
      icon: DatabaseIcon,
    },
  ]
}

function ObservationRegister({
  observation,
  status,
  error,
  onRetry,
}: ObservationRegisterProps) {
  const { locale, t } = useLocale()
  const errorMessage =
    error?.message === "Stats request failed."
      ? t("dashboard.register.requestFailed")
      : error?.message
  const statusLabel =
    status === "live"
      ? t("dashboard.register.live")
      : status === "calibrating"
        ? t("dashboard.register.calibrating")
        : t("dashboard.register.unavailable")

  return (
    <section
      className={styles.observationRegister}
      aria-label={t("dashboard.register.label")}
      aria-busy={status === "calibrating"}
    >
      <div className={styles.registerStatus} data-status={status} aria-live="polite" aria-atomic="true">
        <span aria-hidden="true" />
        <div>
          <small>{t("dashboard.register.feed")}</small>
          <strong>{statusLabel}</strong>
        </div>
      </div>

      <dl className={styles.registerCoordinates}>
        <div>
          <dt>{t("dashboard.register.run")}</dt>
          <dd translate="no">{observation.runId}</dd>
        </div>
        <div>
          <dt>{t("dashboard.register.collection")}</dt>
          <dd title={observation.collection} translate="no">{observation.collection.replace(/^magicforge_/, "")}</dd>
        </div>
        <div>
          <dt>{t("dashboard.register.observed")}</dt>
          <dd>{formatDate(observation.generatedAt, locale)}</dd>
        </div>
      </dl>

      {error && status === "unavailable" ? (
        <div className={styles.registerFault} role="alert">
          <span>{errorMessage} {t("dashboard.register.fallback")}</span>
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            <RotateCwIcon data-icon="inline-start" aria-hidden="true" />
            {t("dashboard.register.recalibrate")}
          </Button>
        </div>
      ) : null}
    </section>
  )
}

function MeridianIntelligenceEngine({ observation }: { observation: CorpusObservation }) {
  const { locale, t } = useLocale()
  const stages = buildMeridianStages(observation, t, locale)
  const [activeStageId, setActiveStageId] = useState<MeridianStageId>("evidence")
  const activeStage = stages.find((stage) => stage.id === activeStageId) ?? stages[0]

  return (
    <section className={styles.meridianEngine} aria-labelledby="meridian-title">
      <div className={styles.engineHeader}>
        <div>
          <span className={styles.instrumentNumber}>{t("dashboard.engine.instrument")}</span>
          <p><TelescopeIcon aria-hidden="true" /> {t("dashboard.engine.name")}</p>
        </div>
        <div className={styles.engineScope}>
          <span>{t("dashboard.engine.chain")}</span>
          <strong translate="no">{observation.mode.toUpperCase()}</strong>
        </div>
      </div>

      <div className={styles.meridianChamber}>
        <div className={styles.meridianScale} aria-hidden="true" />
        <div className={styles.meridianArc} aria-hidden="true">
          <span />
        </div>
        <div className={styles.stageSequence}>
          {stages.map((stage) => {
            const Icon = stage.icon
            const active = stage.id === activeStage.id
            return (
              <Link
                key={stage.id}
                href={stage.href}
                className={styles.meridianStage}
                data-active={active}
                onPointerEnter={() => setActiveStageId(stage.id)}
                onFocus={() => setActiveStageId(stage.id)}
                aria-describedby="meridian-reading"
              >
                <span className={styles.stageIndex}>{stage.index}</span>
                <span className={styles.stageDial} aria-hidden="true">
                  <Icon />
                  <i />
                </span>
                <span className={styles.stageAction}>{stage.action}</span>
                <strong>{formatCount(stage.value, locale)}</strong>
                <span className={styles.stageLabel}>{stage.label}</span>
              </Link>
            )
          })}
        </div>
      </div>

      <div id="meridian-reading" className={styles.engineReading} aria-live="polite">
        <span className={styles.readingIndex}>{activeStage.index}</span>
        <div>
          <small>{activeStage.action} / {t("dashboard.engine.activeReading")}</small>
          <h2 id="meridian-title">{activeStage.label}</h2>
          <p>{activeStage.detail}</p>
        </div>
        <Link href={activeStage.href} className={styles.readingFootnote}>
          <strong>{activeStage.footnote}</strong>
          <span>{t("dashboard.engine.openInstrument")} <i aria-hidden="true">↗</i></span>
        </Link>
      </div>
    </section>
  )
}

function ObservationLedger({ observation }: { observation: CorpusObservation }) {
  const { locale, t } = useLocale()

  return (
    <section className={styles.observationLedger} aria-labelledby="ledger-title">
      <div className={styles.ledgerHeading}>
        <RadioTowerIcon aria-hidden="true" />
        <div>
          <span>{t("dashboard.ledger.instrument")}</span>
          <h2 id="ledger-title">{t("dashboard.ledger.title")}</h2>
        </div>
      </div>
      <div className={styles.ledgerRail} aria-hidden="true">
        <span className={styles.ledgerOrigin}>{t("dashboard.ledger.noPriorRun")}</span>
        <i />
        <span className={styles.ledgerObservation} translate="no">OBS / {observation.runId}</span>
      </div>
      <div className={styles.ledgerEntry}>
        <time dateTime={observation.generatedAt}>
          {formatDate(observation.generatedAt, locale)}
        </time>
        <p>{t("dashboard.ledger.description")}</p>
        <span translate="no">Manifest {observation.manifestId.slice(0, 8).toUpperCase()}</span>
      </div>
      <div className={styles.ledgerReserved} aria-hidden="true">
        {t("dashboard.ledger.future")}
      </div>
    </section>
  )
}

function GovernanceCounterweight({ observation }: { observation: CorpusObservation }) {
  const { locale, t } = useLocale()
  const untouched = !observation.governance.productionCollectionTouched

  return (
    <section className={styles.governanceInstrument} aria-labelledby="governance-title">
      <div className={styles.governanceHeading}>
        <ScaleIcon aria-hidden="true" />
        <div>
          <span>{t("dashboard.governance.instrument")}</span>
          <h2 id="governance-title">{t("dashboard.governance.title")}</h2>
        </div>
      </div>

      <div className={styles.balanceAssembly} aria-hidden="true">
        <div className={styles.balanceFulcrum} />
        <div className={styles.balanceBeam} />
        <div className={styles.balancePan} data-side="projection">
          <strong>{formatCount(observation.counts.fragments, locale)}</strong>
          <span>{t("dashboard.governance.projected")}</span>
        </div>
        <div className={styles.balancePan} data-side="verified">
          <strong>{formatCount(observation.counts.humanVerified, locale)}</strong>
          <span>{t("dashboard.governance.verified")}</span>
        </div>
      </div>

      <dl className={styles.governanceLedger}>
        <div>
          <dt>{t("dashboard.governance.pendingSources")}</dt>
          <dd>{formatCount(observation.governance.pendingHumanReviewSources, locale)}</dd>
        </div>
        <div>
          <dt>{t("dashboard.governance.pendingContradictions")}</dt>
          <dd>{formatCount(observation.governance.contradictionChecksPending, locale)}</dd>
        </div>
        <div>
          <dt>{t("dashboard.governance.quarantined")}</dt>
          <dd>{formatCount(observation.governance.quarantinedMethods, locale)}</dd>
        </div>
        <div>
          <dt>{t("dashboard.governance.productionCollection")}</dt>
          <dd data-safe={untouched}>
            {untouched
              ? t("dashboard.governance.untouched")
              : t("dashboard.governance.touched")}
          </dd>
        </div>
      </dl>
    </section>
  )
}

export function DashboardOverview() {
  const { locale, t } = useLocale()
  const [liveStats, setLiveStats] = useState<CorpusStatsResponse | null>(null)
  const [statsError, setStatsError] = useState<MagicForgeApiError | null>(null)
  const [calibrating, setCalibrating] = useState(true)
  const [calibrationAttempt, setCalibrationAttempt] = useState(0)

  useEffect(() => {
    let active = true

    magicForgeApi
      .stats()
      .then((stats) => {
        if (active) {
          setLiveStats(stats)
          setStatsError(null)
        }
      })
      .catch((cause) => {
        if (!active) return
        setStatsError(
          cause instanceof MagicForgeApiError
            ? cause
            : new MagicForgeApiError("Stats request failed.", "backend_error", 500)
        )
      })
      .finally(() => {
        if (active) setCalibrating(false)
      })

    return () => {
      active = false
    }
  }, [calibrationAttempt])

  function recalibrate() {
    setCalibrating(true)
    setStatsError(null)
    setCalibrationAttempt((attempt) => attempt + 1)
  }

  const observation = liveStats ? observationFromStats(liveStats) : null

  if (!observation) {
    return (
      <MotionPage className={styles.room}>
        <div className={styles.roomLight} aria-hidden="true" />
        <div className={styles.roomGrid} aria-hidden="true" />
        <div className={styles.observatoryShell}>
          <header className={styles.observatoryHero}>
            <div className={styles.heroCopy}>
              <p className={styles.heroKicker}>
                <ApertureIcon aria-hidden="true" /> {t("dashboard.hero.kicker")}
              </p>
              <h1>{t("dashboard.hero.title")}</h1>
              <p>{t("dashboard.hero.description")}</p>
            </div>
            <div className={styles.heroInstrument}>
              <span className={styles.heroInstrumentTicks} aria-hidden="true" />
              <div>
                <BoxesIcon aria-hidden="true" />
                <strong aria-label={t("dashboard.register.noLiveReading")}>—</strong>
                <span>{t("dashboard.hero.fragments")}</span>
              </div>
            </div>
          </header>

          <section
            className={styles.observationRegister}
            aria-label={t("dashboard.register.label")}
            aria-busy={calibrating}
          >
            <div
              className={styles.registerStatus}
              data-status={calibrating ? "calibrating" : "unavailable"}
              aria-live="polite"
            >
              <span aria-hidden="true" />
              <div>
                <small>{t("dashboard.register.feed")}</small>
                <strong>
                  {calibrating
                    ? t("dashboard.register.calibrating")
                    : t("dashboard.register.unavailable")}
                </strong>
              </div>
            </div>
            {statsError ? (
              <div className={styles.registerFault}>
                <EndpointNotice error={statsError} route="GET /stats" compact />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={calibrating}
                  onClick={recalibrate}
                >
                  <RotateCwIcon data-icon="inline-start" aria-hidden="true" />
                  {t("dashboard.register.recalibrate")}
                </Button>
              </div>
            ) : (
              <p role="status">{t("dashboard.register.waitingForLive")}</p>
            )}
          </section>
        </div>
      </MotionPage>
    )
  }

  return (
    <MotionPage className={styles.room}>
      <div className={styles.roomLight} aria-hidden="true" />
      <div className={styles.roomGrid} aria-hidden="true" />

      <div className={styles.observatoryShell}>
        <header className={styles.observatoryHero}>
          <div className={styles.heroCopy}>
            <p className={styles.heroKicker}>
              <ApertureIcon aria-hidden="true" /> {t("dashboard.hero.kicker")}
            </p>
            <h1>{t("dashboard.hero.title")}</h1>
            <p>{t("dashboard.hero.description")}</p>
          </div>

          <div className={styles.heroInstrument}>
            <span className={styles.heroInstrumentTicks} aria-hidden="true" />
            <div>
              <BoxesIcon aria-hidden="true" />
              <strong>{formatCount(observation.counts.fragments, locale)}</strong>
              <span>{t("dashboard.hero.fragments")}</span>
            </div>
          </div>
        </header>

        <ObservationRegister
          observation={observation}
          status={calibrating ? "calibrating" : statsError ? "unavailable" : "live"}
          error={statsError}
          onRetry={recalibrate}
        />

        <MeridianIntelligenceEngine observation={observation} />
        <CorpusInstruments observation={observation} />

        <div className={styles.lowerInstruments}>
          <ObservationLedger observation={observation} />
          <GovernanceCounterweight observation={observation} />
        </div>

        <footer className={styles.observatoryFooter}>
          <ShieldAlertIcon aria-hidden="true" />
          <p>{t("dashboard.footer")}</p>
        </footer>
      </div>
    </MotionPage>
  )
}
