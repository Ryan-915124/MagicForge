"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type { LucideIcon } from "lucide-react"
import {
  ArchiveIcon,
  AtomIcon,
  BeakerIcon,
  BrainCircuitIcon,
  DatabaseIcon,
  FileSearchIcon,
  FlaskConicalIcon,
  LockKeyholeIcon,
  NetworkIcon,
  PackageCheckIcon,
  RadioTowerIcon,
  RefreshCwIcon,
  ScanLineIcon,
  ShieldAlertIcon,
  WaypointsIcon,
} from "lucide-react"

import { MotionPage } from "@/components/app-shell/motion-page"
import { useLocale } from "@/components/i18n/locale-provider"
import styles from "@/components/research/research-laboratory.module.css"
import { EndpointNotice } from "@/components/shared/endpoint-notice"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { MagicForgeApiError, magicForgeApi } from "@/lib/api/client"
import type {
  ResearchConsoleResponse,
  ResearchPipelineStage,
} from "@/lib/api/types"
import { formatCount, formatDate, humanize } from "@/lib/format"
import type { MessageKey } from "@/lib/i18n/messages"

type ApparatusStageId =
  | "discovery"
  | "sources"
  | "extraction"
  | "evidence"
  | "knowledge"
  | "memory"

interface ApparatusStageDefinition {
  id: ApparatusStageId
  ordinal: string
  titleKey: MessageKey
  shortReadingKey: MessageKey
  descriptionKey: MessageKey
  icon: LucideIcon
}

interface InspectionReading {
  labelKey: MessageKey
  value: number
}

const APPARATUS_STAGES: readonly ApparatusStageDefinition[] = [
  {
    id: "discovery",
    ordinal: "01",
    titleKey: "research.stage.discovery.title",
    shortReadingKey: "research.stage.discovery.shortReading",
    descriptionKey: "research.stage.discovery.description",
    icon: FileSearchIcon,
  },
  {
    id: "sources",
    ordinal: "02",
    titleKey: "research.stage.sources.title",
    shortReadingKey: "research.stage.sources.shortReading",
    descriptionKey: "research.stage.sources.description",
    icon: ArchiveIcon,
  },
  {
    id: "extraction",
    ordinal: "03",
    titleKey: "research.stage.extraction.title",
    shortReadingKey: "research.stage.extraction.shortReading",
    descriptionKey: "research.stage.extraction.description",
    icon: BeakerIcon,
  },
  {
    id: "evidence",
    ordinal: "04",
    titleKey: "research.stage.evidence.title",
    shortReadingKey: "research.stage.evidence.shortReading",
    descriptionKey: "research.stage.evidence.description",
    icon: PackageCheckIcon,
  },
  {
    id: "knowledge",
    ordinal: "05",
    titleKey: "research.stage.knowledge.title",
    shortReadingKey: "research.stage.knowledge.shortReading",
    descriptionKey: "research.stage.knowledge.description",
    icon: NetworkIcon,
  },
  {
    id: "memory",
    ordinal: "06",
    titleKey: "research.stage.memory.title",
    shortReadingKey: "research.stage.memory.shortReading",
    descriptionKey: "research.stage.memory.description",
    icon: DatabaseIcon,
  },
] as const

function indexStages(stages: ResearchPipelineStage[]) {
  return Object.fromEntries(stages.map((stage) => [stage.id, stage]))
}

function metric(stage: ResearchPipelineStage | undefined, key: string) {
  return stage?.metrics[key] ?? 0
}

function readingsForStage(
  stageId: ApparatusStageId,
  stages: Record<string, ResearchPipelineStage | undefined>
): InspectionReading[] {
  switch (stageId) {
    case "discovery":
      return [
        {
          labelKey: "research.metric.queriesExecuted",
          value: metric(stages.discovery, "queries_executed"),
        },
        {
          labelKey: "research.metric.rawResults",
          value: metric(stages.discovery, "raw_results"),
        },
        {
          labelKey: "research.metric.verifiedNewSources",
          value: metric(stages.discovery, "verified_new_sources"),
        },
      ]
    case "sources":
      return [
        {
          labelKey: "research.metric.registeredSources",
          value: metric(stages.source_registry, "sources_processed"),
        },
      ]
    case "extraction":
      return [
        {
          labelKey: "research.metric.sourcesExtracted",
          value: metric(stages.extraction, "sources_extracted"),
        },
        {
          labelKey: "research.metric.claimsGenerated",
          value: metric(stages.extraction, "claims_generated"),
        },
        {
          labelKey: "research.metric.extractionErrors",
          value: metric(stages.extraction, "extraction_errors"),
        },
      ]
    case "evidence":
      return [
        {
          labelKey: "research.metric.cardsGenerated",
          value: metric(stages.evidence_forge, "generated"),
        },
        {
          labelKey: "research.metric.cardsProjected",
          value: metric(stages.evidence_forge, "projected"),
        },
      ]
    case "knowledge":
      return [
        {
          labelKey: "research.metric.nodesGenerated",
          value: metric(stages.knowledge_assembly, "generated"),
        },
        {
          labelKey: "research.metric.nodesProjected",
          value: metric(stages.knowledge_assembly, "projected"),
        },
        {
          labelKey: "research.metric.relationsProjected",
          value: metric(stages.relationship_gate, "projected"),
        },
        {
          labelKey: "research.metric.semanticRejections",
          value: metric(stages.knowledge_assembly, "semantic_rejections"),
        },
      ]
    case "memory":
      return [
        {
          labelKey: "research.metric.receiptBackedPoints",
          value: metric(stages.memory_projection, "qdrant_points"),
        },
      ]
  }
}

function primaryStageReading(
  stageId: ApparatusStageId,
  stages: Record<string, ResearchPipelineStage | undefined>
) {
  switch (stageId) {
    case "discovery":
      return metric(stages.discovery, "raw_results")
    case "sources":
      return metric(stages.source_registry, "sources_processed")
    case "extraction":
      return metric(stages.extraction, "claims_generated")
    case "evidence":
      return metric(stages.evidence_forge, "projected")
    case "knowledge":
      return metric(stages.knowledge_assembly, "projected")
    case "memory":
      return metric(stages.memory_projection, "qdrant_points")
  }
}

function LaboratoryHeader({
  observation,
  loading,
  stale,
  onRefresh,
}: {
  observation: ResearchConsoleResponse | null
  loading: boolean
  stale: boolean
  onRefresh: () => void
}) {
  const { locale, t } = useLocale()

  return (
    <header className={styles.hero}>
      <div className={styles.heroCopy}>
        <p className={styles.eyebrow}>
          <FlaskConicalIcon aria-hidden="true" /> {t("research.header.eyebrow")}
        </p>
        <h1>{t("research.header.title")}</h1>
        <p className={styles.heroDescription}>{t("research.header.description")}</p>
      </div>

      <aside className={styles.doorPlaque} aria-label={t("research.header.currentSpecimenLabel")}>
        <small>
          {stale
            ? t("research.header.staleSpecimen")
            : t("research.header.currentSpecimen")}
        </small>
        <strong>{observation?.current_run.run_id ?? t("research.header.unavailable")}</strong>
        <dl className={styles.plaqueCoordinates}>
          <div>
            <dt>{t("research.header.recorded")}</dt>
            <dd>
              {observation
                ? formatDate(observation.current_run.generated_at, locale)
                : t("research.header.noReading")}
            </dd>
          </div>
          <div>
            <dt>{t("research.header.governanceMode")}</dt>
            <dd>{observation?.current_run.mode ?? t("research.header.unknown")}</dd>
          </div>
        </dl>
        <Button
          className={styles.refreshButton}
          variant="outline"
          onClick={onRefresh}
          disabled={loading}
          data-scanning={loading}
        >
          <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
          {loading ? t("research.header.scanning") : t("research.header.rescan")}
        </Button>
      </aside>
    </header>
  )
}

function KnowledgeRig({ observation }: { observation: ResearchConsoleResponse }) {
  const { locale, t } = useLocale()
  const [selectedStageId, setSelectedStageId] = useState<ApparatusStageId>("discovery")
  const inspectionRef = useRef<HTMLDivElement>(null)
  const indexedStages = useMemo(
    () => indexStages(observation.pipeline.stages),
    [observation.pipeline.stages]
  )
  const selectedDefinition =
    APPARATUS_STAGES.find((stage) => stage.id === selectedStageId) ?? APPARATUS_STAGES[0]
  const readings = readingsForStage(selectedStageId, indexedStages)

  return (
    <section className={styles.rig} aria-labelledby="knowledge-rig-title">
      <div className={styles.rigHeader}>
        <div>
          <p className={styles.rigEyebrow}>
            <WaypointsIcon aria-hidden="true" /> {t("research.pipeline.eyebrow")}
          </p>
          <h2 id="knowledge-rig-title">{t("research.pipeline.title")}</h2>
        </div>
        <div className={styles.rigMode}>
          <span>{t("research.pipeline.readOnly")}</span>
          {humanize(observation.pipeline.status)}
        </div>
      </div>

      <div className={styles.rigBody}>
        <ToggleGroup
          className={styles.stageSelector}
          value={[selectedStageId]}
          onValueChange={(value) => {
            const next = value[0] as ApparatusStageId | undefined
            if (!next) return
            setSelectedStageId(next)
            if (window.matchMedia("(max-width: 680px)").matches) {
              window.requestAnimationFrame(() => {
                inspectionRef.current?.scrollIntoView({
                  behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
                    ? "auto"
                    : "smooth",
                  block: "start",
                })
              })
            }
          }}
          aria-label={t("research.pipeline.inspectLabel")}
        >
          {APPARATUS_STAGES.map((stage) => {
            const Icon = stage.icon
            return (
              <ToggleGroupItem
                key={stage.id}
                className={styles.rigStage}
                value={stage.id}
                data-stage={stage.id}
                aria-label={t("research.pipeline.inspectStage", {
                  stage: t(stage.titleKey),
                })}
              >
                <span className={styles.stageMachine} aria-hidden="true">
                  <Icon />
                </span>
                <span className={styles.stageOrdinal}>
                  {t("research.pipeline.chamber", { number: stage.ordinal })}
                </span>
                <strong>{t(stage.titleKey)}</strong>
                <span className={styles.stageCount}>
                  {formatCount(primaryStageReading(stage.id, indexedStages), locale)} · {t(stage.shortReadingKey)}
                </span>
              </ToggleGroupItem>
            )
          })}
        </ToggleGroup>
      </div>

      <div ref={inspectionRef} className={styles.inspectionWindow} aria-live="polite">
        <span className={styles.inspectionIndex} aria-hidden="true">
          {selectedDefinition.ordinal}
        </span>
        <div>
          <span className={styles.inspectionLabel}>{t("research.pipeline.inspectionWindow")}</span>
          <h3>{t(selectedDefinition.titleKey)}</h3>
          <p>{t(selectedDefinition.descriptionKey)}</p>
        </div>
        <dl className={styles.inspectionReadings}>
          {readings.map((reading) => (
            <div key={reading.labelKey}>
              <dt>{t(reading.labelKey)}</dt>
              <dd>{formatCount(reading.value, locale)}</dd>
            </div>
          ))}
        </dl>
      </div>

      <div className={styles.rejectTray}>
        <span>{t("research.pipeline.rejectionTray")}</span>
        <strong>{t("research.pipeline.rejectionCount", {
          count: formatCount(metric(indexedStages.knowledge_assembly, "semantic_rejections"), locale),
        })}</strong>
      </div>
    </section>
  )
}

function SignalCoupler({ observation }: { observation: ResearchConsoleResponse }) {
  const { locale, t } = useLocale()
  const contractObserved = observation.runtime.api.status === "ok"

  return (
    <section className={styles.instrument} aria-labelledby="signal-coupler-title">
      <div className={styles.instrumentHeader}>
        <p className={styles.instrumentEyebrow}>
          <RadioTowerIcon aria-hidden="true" /> {t("research.instrument.apiEyebrow")}
        </p>
        <h2 id="signal-coupler-title">{t("research.instrument.apiTitle")}</h2>
        <span className={styles.instrumentMeta}>{t("research.instrument.apiMeta")}</span>
      </div>
      <div className={styles.couplerBody} aria-hidden="true">
        <div className={styles.couplerPlate}>
          {Array.from({ length: 6 }, (_, index) => (
            <span key={index} className={styles.couplerPin} data-live={contractObserved} />
          ))}
        </div>
      </div>
      <div className={styles.instrumentFooter}>
        <span className={styles.instrumentState}>{t("research.instrument.contractObserved")}</span>
        <span>{formatDate(observation.observed_at, locale)}</span>
      </div>
    </section>
  )
}

function IntelligenceOptic({ observation }: { observation: ResearchConsoleResponse }) {
  const { t } = useLocale()
  const instrument = observation.runtime.intelligence_instrument

  return (
    <section className={styles.instrument} aria-labelledby="intelligence-optic-title">
      <div className={styles.instrumentHeader}>
        <p className={styles.instrumentEyebrow}>
          <BrainCircuitIcon aria-hidden="true" /> {t("research.instrument.intelligenceEyebrow")}
        </p>
        <h2 id="intelligence-optic-title">{instrument.provider}</h2>
        <span className={styles.instrumentMeta}>
          {instrument.model} · {instrument.structured_extraction
            ? t("research.instrument.structuredEnabled")
            : t("research.instrument.structuredDisabled")}
        </span>
      </div>
      <div className={styles.opticBody} aria-hidden="true">
        <div className={styles.opticLens}>
          <AtomIcon />
        </div>
      </div>
      <div className={styles.instrumentFooter}>
        <span
          className={styles.instrumentState}
          data-state={instrument.configured ? undefined : "attention"}
        >
          {instrument.configured
            ? t("research.instrument.configured")
            : t("research.instrument.configurationMissing")}
        </span>
        <span>{t("research.instrument.connectivity", {
          status: humanize(instrument.connectivity),
        })}</span>
      </div>
    </section>
  )
}

function MemoryCylinder({ observation }: { observation: ResearchConsoleResponse }) {
  const { locale, t } = useLocale()
  const { memory_vault: memory } = observation
  const mismatch = memory.alignment_status === "configuration_mismatch"

  return (
    <section className={styles.instrument} aria-labelledby="memory-cylinder-title">
      <div className={styles.instrumentHeader}>
        <p className={styles.instrumentEyebrow}>
          <DatabaseIcon aria-hidden="true" /> {t("research.memory.eyebrow")}
        </p>
        <h2 id="memory-cylinder-title">{t("research.memory.title")}</h2>
        <span className={styles.instrumentMeta}>{t("research.memory.meta")}</span>
      </div>
      <div className={styles.memoryBody}>
        <div
          className={styles.memoryCylinder}
          aria-label={t("research.memory.cylinderLabel", {
            count: formatCount(memory.receipt.point_count, locale),
          })}
        >
          <span className={styles.memoryFill} aria-hidden="true" />
          <span className={styles.cylinderCaption}>{t("research.memory.sealedSpecimen")}</span>
          <strong>{formatCount(memory.receipt.point_count, locale)}</strong>
        </div>
        <dl className={styles.memoryReadings}>
          <div>
            <dt>{t("research.memory.runtimeChannel")}</dt>
            <dd data-mismatch={mismatch}>{memory.runtime_collection}</dd>
          </div>
          <div>
            <dt>{t("research.memory.auditedVault")}</dt>
            <dd>{memory.audited_collection}</dd>
          </div>
          <div>
            <dt>{t("research.memory.alignment")}</dt>
            <dd data-mismatch={mismatch}>{humanize(memory.alignment_status)}</dd>
          </div>
          <div>
            <dt>{t("research.memory.smokeSpecimen")}</dt>
            <dd>
              {t("research.memory.smokeReading", {
                queries: memory.retrieval_smoke.query_count,
                points: memory.retrieval_smoke.collection_count,
              })}
            </dd>
          </div>
        </dl>
      </div>
      <div className={styles.instrumentFooter}>
        <span className={styles.instrumentState} data-state={mismatch ? "attention" : undefined}>
          {mismatch ? t("research.memory.calibrationRequired") : t("research.memory.aligned")}
        </span>
        <span>{t("research.memory.countCaveat")}</span>
      </div>
    </section>
  )
}

function InstrumentDeck({ observation }: { observation: ResearchConsoleResponse }) {
  const { t } = useLocale()

  return (
    <div className={styles.instrumentDeck} aria-label={t("research.instrument.deckLabel")}>
      <SignalCoupler observation={observation} />
      <IntelligenceOptic observation={observation} />
      <MemoryCylinder observation={observation} />
    </div>
  )
}

function CuratorInterlock({ observation }: { observation: ResearchConsoleResponse }) {
  const { locale, t } = useLocale()
  const { governance, memory_vault: memory } = observation

  return (
    <section className={styles.interlock} aria-labelledby="curator-interlock-title">
      <div className={styles.interlockHeading}>
        <LockKeyholeIcon aria-hidden="true" />
        <div>
          <span>{t("research.governance.eyebrow")}</span>
          <h2 id="curator-interlock-title">{t("research.governance.title")}</h2>
        </div>
      </div>
      <Accordion defaultValue={["checkpoint"]}>
        <AccordionItem value="checkpoint">
          <AccordionTrigger className={styles.interlockTrigger}>
            {t("research.governance.releaseGate")} · {humanize(governance.checkpoint_status)}
          </AccordionTrigger>
          <AccordionContent className={styles.interlockContent}>
            <div className={styles.sealReading}>
              <div className={styles.sealDial} aria-label={t("research.governance.zeroVerifiedLabel")}>
                <strong>{formatCount(governance.human_verified_points, locale)}</strong>
              </div>
              <div className={styles.sealCopy}>
                <h3>{t("research.governance.noVerification")}</h3>
                <p>{t("research.governance.description")}</p>
              </div>
            </div>
            <dl className={styles.governanceMatrix}>
              <div>
                <dt>{t("research.governance.sourcesPending")}</dt>
                <dd>{formatCount(governance.sources_pending, locale)}</dd>
              </div>
              <div>
                <dt>{t("research.governance.evidencePending")}</dt>
                <dd>{formatCount(governance.evidence_cards_pending, locale)}</dd>
              </div>
              <div>
                <dt>{t("research.governance.knowledgePending")}</dt>
                <dd>{formatCount(governance.knowledge_nodes_pending, locale)}</dd>
              </div>
              <div>
                <dt>{t("research.governance.relationsPending")}</dt>
                <dd>{formatCount(governance.relationships_pending, locale)}</dd>
              </div>
              <div>
                <dt>{t("research.governance.contradictionsPending")}</dt>
                <dd>{formatCount(governance.contradiction_checks_pending, locale)}</dd>
              </div>
              <div>
                <dt>{t("research.governance.methodsQuarantined")}</dt>
                <dd>{formatCount(governance.procedural_method_projections_quarantined, locale)}</dd>
              </div>
            </dl>
            <div className={styles.productionSeal}>
              <ShieldAlertIcon aria-hidden="true" />
              {t(
                memory.safety.production_collection_touched
                  ? "research.governance.productionTouched"
                  : "research.governance.productionUntouched",
                {
                  approved: formatCount(governance.approved_points, locale),
                  permitted: formatCount(governance.storage_permission_points, locale),
                }
              )}
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </section>
  )
}

function RunArchive({ observation }: { observation: ResearchConsoleResponse }) {
  const { locale, t } = useLocale()
  const [selectedRunId, setSelectedRunId] = useState(observation.current_run.run_id)
  const selectedRun =
    observation.run_history.find((run) => run.run_id === selectedRunId) ??
    observation.run_history.at(-1)

  if (!selectedRun) return null
  const generatedBasis = selectedRun.metric_basis === "reported_generated_outputs"
  const metricBasisLabel = generatedBasis
    ? t("research.runs.generatedBasis")
    : t("research.runs.projectedBasis")

  return (
    <section className={styles.runArchive} aria-labelledby="run-archive-title">
      <div className={styles.runHeader}>
        <ScanLineIcon aria-hidden="true" />
        <div>
          <span>{t("research.runs.eyebrow")}</span>
          <h2 id="run-archive-title">{t("research.runs.title")}</h2>
        </div>
      </div>
      <ToggleGroup
        className={styles.runSelector}
        value={[selectedRun.run_id]}
        onValueChange={(value) => {
          if (value[0]) setSelectedRunId(value[0])
        }}
        aria-label={t("research.runs.inspectLabel")}
      >
        {observation.run_history.map((run, index) => (
          <ToggleGroupItem
            key={run.run_id}
            value={run.run_id}
            className={styles.runCartridge}
            aria-label={t("research.runs.inspectRun", { run: run.run_id })}
          >
            <span className={styles.runMeta}>
              {t("research.runs.specimen", { number: String(index + 1).padStart(2, "0") })}
            </span>
            <strong>{run.run_id}</strong>
            <span className={styles.runMeta}>
              {t("research.runs.points", { count: formatCount(run.qdrant_points, locale) })}
            </span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      <div className={styles.runDetail} aria-live="polite">
        <div className={styles.runIdentity}>
          <span className={styles.inspectionLabel}>{t("research.runs.selected")}</span>
          <h3>{selectedRun.run_id}</h3>
          <p>
            {formatDate(selectedRun.generated_at, locale)}
            <br />
            {selectedRun.collection}
            <br />
            {humanize(selectedRun.status)} · {t("research.runs.extractionErrors", {
              count: selectedRun.extraction_errors,
            })}
            <br />
            {t("research.runs.metricBasis")} · {metricBasisLabel}
          </p>
        </div>
        <dl className={styles.runMetrics}>
          <RunMetric label={t("research.runs.sources")} value={selectedRun.sources} />
          <RunMetric label={t("research.runs.claims")} value={selectedRun.claims} />
          <RunMetric
            label={generatedBasis
              ? t("research.runs.evidenceGenerated")
              : t("research.runs.evidenceProjected")}
            value={selectedRun.evidence_cards}
          />
          <RunMetric
            label={generatedBasis
              ? t("research.runs.conceptsGenerated")
              : t("research.runs.conceptsProjected")}
            value={selectedRun.knowledge_nodes}
          />
          <RunMetric
            label={generatedBasis
              ? t("research.runs.relationsGenerated")
              : t("research.runs.relationsProjected")}
            value={selectedRun.relationships}
          />
          <RunMetric
            label={generatedBasis
              ? t("research.runs.pointsReported")
              : t("research.runs.pointsReceipted")}
            value={selectedRun.qdrant_points}
          />
        </dl>
      </div>
      <p className={styles.runCaveat}>{t("research.runs.caveat")}</p>
    </section>
  )
}

function RunMetric({ label, value }: { label: string; value: number }) {
  const { locale } = useLocale()

  return (
    <div>
      <dt>{label}</dt>
      <dd>{formatCount(value, locale)}</dd>
    </div>
  )
}

function LaboratoryLoading() {
  const { t } = useLocale()

  return (
    <div className={styles.loadingRig} aria-label={t("research.loading")}>
      <Skeleton />
      <Skeleton />
      <Skeleton />
    </div>
  )
}

export function ResearchConsole() {
  const { t } = useLocale()
  const [observation, setObservation] = useState<ResearchConsoleResponse | null>(null)
  const [error, setError] = useState<MagicForgeApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const translateRef = useRef(t)

  useEffect(() => {
    translateRef.current = t
  }, [t])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setObservation(await magicForgeApi.researchConsole())
    } catch (cause) {
      setError(
        cause instanceof MagicForgeApiError
          ? cause
          : new MagicForgeApiError(
              translateRef.current("research.error.observationFailed"),
              "backend_error",
              500
            )
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    magicForgeApi
      .researchConsole()
      .then((response) => {
        if (active) setObservation(response)
      })
      .catch((cause) => {
        if (!active) return
        setError(
          cause instanceof MagicForgeApiError
            ? cause
            : new MagicForgeApiError(
                translateRef.current("research.error.observationFailed"),
                "backend_error",
                500
              )
        )
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <MotionPage className={styles.laboratory}>
      <div className={styles.shell} aria-busy={loading}>
        <LaboratoryHeader
          observation={observation}
          loading={loading}
          stale={Boolean(error && observation)}
          onRefresh={() => void refresh()}
        />

        {error && (
          <div className={styles.faultDock}>
            <EndpointNotice error={error} route="GET /research/console" compact />
          </div>
        )}

        {!observation && loading && <LaboratoryLoading />}

        {observation && (
          <>
            <KnowledgeRig observation={observation} />
            <InstrumentDeck observation={observation} />
            <div className={styles.lowerBench}>
              <CuratorInterlock observation={observation} />
              <RunArchive observation={observation} />
            </div>
          </>
        )}
      </div>
    </MotionPage>
  )
}
