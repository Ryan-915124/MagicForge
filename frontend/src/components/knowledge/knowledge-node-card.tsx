import Link from "next/link"
import {
  ArrowUpRightIcon,
  BookOpenCheckIcon,
  BrainCircuitIcon,
  CircleDashedIcon,
  CompassIcon,
  HistoryIcon,
  NetworkIcon,
  RouteIcon,
  ShieldAlertIcon,
  SparklesIcon,
  WrenchIcon,
} from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { ConfidenceBadge } from "@/components/shared/confidence-badge"
import { OriginBadge } from "@/components/shared/origin-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import type {
  EvidenceCard,
  KnowledgeNodeVersion,
  KnowledgeOrigin,
  KnowledgeRelationship,
} from "@/lib/api/types"
import { humanize } from "@/lib/format"
import type { MessageKey } from "@/lib/i18n/messages"

import styles from "./knowledge-explorer.module.css"

const epistemicLayers: Array<{
  origin: KnowledgeOrigin
  label: string
  descriptionKey: MessageKey
}> = [
  {
    origin: "scientific_evidence",
    label: "Scientific Evidence",
    descriptionKey: "knowledge.panel.scientificDescription",
  },
  {
    origin: "expert_practice",
    label: "Expert Practice",
    descriptionKey: "knowledge.panel.practiceDescription",
  },
  {
    origin: "personal_interpretation",
    label: "MagicForge Interpretation",
    descriptionKey: "knowledge.panel.interpretationDescription",
  },
]

function historicalContext(node: KnowledgeNodeVersion) {
  const attributes = node.entity.attributes
  for (const key of ["historical_context", "historical_note", "history"]) {
    const value = attributes[key]
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return null
}

function evidenceHref(id: string) {
  const params = new URLSearchParams({ browse: "1", evidence: id })
  return `/evidence?${params.toString()}`
}

function EvidenceTrace({ card }: { card: EvidenceCard }) {
  const { t } = useLocale()
  const sealed = card.secret_exposure_level === "method_detail" || card.secret_exposure_level === "operational_secret"
  const confidence = card.confidence

  return (
    <Link href={evidenceHref(card.id)} className={styles.evidenceTrace}>
      <span className={styles.evidenceTraceIndex}>EV / {card.id.slice(0, 8).toUpperCase()}</span>
      <strong>{sealed ? t("knowledge.panel.restricted") : card.claim}</strong>
      <span className={styles.evidenceTraceMeta}>
        {humanize(card.evidence_class)}
        <i aria-hidden="true" />
        {confidence
          ? t("knowledge.panel.assessment", { label: confidence.label })
          : t("knowledge.panel.notAssessed")}
        <i aria-hidden="true" />
        {card.source.source_year ?? t("knowledge.panel.yearUnrecorded")}
      </span>
      <ArrowUpRightIcon aria-hidden="true" />
    </Link>
  )
}

interface KnowledgeNodeCardProps {
  node: KnowledgeNodeVersion
  evidenceCards: EvidenceCard[]
  evidenceLoading: boolean
  evidenceFailed: boolean
  relatedNodes: KnowledgeNodeVersion[]
  relationships: KnowledgeRelationship[]
  onTraceFromHere: () => void
}

export function KnowledgeNodeCard({
  node,
  evidenceCards,
  evidenceLoading,
  evidenceFailed,
  relatedNodes,
  relationships,
  onTraceFromHere,
}: KnowledgeNodeCardProps) {
  const { t } = useLocale()
  const context = historicalContext(node)
  const nodeById = new Map(relatedNodes.map((related) => [related.entity.id, related]))
  const connectedTechniques = relatedNodes.filter((related) => related.entity.type === "technique")
  const description = node.entity.description?.trim()

  return (
    <article className={styles.artifactDocument}>
      <header className={styles.artifactHeader}>
        <div className={styles.artifactAccession}>
          <span>{t("knowledge.panel.artifact")}</span>
          <code>{node.entity.id.slice(0, 8).toUpperCase()}</code>
        </div>
        <div className={styles.artifactTitleBlock}>
          <Badge variant="outline">{humanize(node.entity.type)}</Badge>
          <h2>{node.entity.name}</h2>
          <p>{node.definition}</p>
        </div>
        <div className={styles.artifactSealRow}>
          <OriginBadge origin={node.knowledge_origin} />
          <ConfidenceBadge label={node.confidence.label} score={node.confidence.score} />
          <Badge variant="outline">
            <ShieldAlertIcon data-icon="inline-start" aria-hidden="true" />
            {node.human_verified ? t("knowledge.panel.humanVerified") : t("knowledge.panel.bootstrapUnverified")}
          </Badge>
        </div>
        <Button type="button" variant="outline" size="lg" onClick={onTraceFromHere}>
          <CompassIcon data-icon="inline-start" aria-hidden="true" />
          {t("knowledge.panel.trace")}
        </Button>
      </header>

      <Separator />

      {description && description !== node.definition ? (
        <section className={styles.artifactSection} aria-labelledby="artifact-description-title">
          <div className={styles.artifactSectionTitle}>
            <SparklesIcon aria-hidden="true" />
            <h3 id="artifact-description-title">{t("knowledge.panel.descriptionTitle")}</h3>
          </div>
          <p>{description}</p>
        </section>
      ) : null}

      <section className={styles.artifactSection} aria-labelledby="artifact-origin-title">
        <div className={styles.artifactSectionTitle}>
          <BookOpenCheckIcon aria-hidden="true" />
          <h3 id="artifact-origin-title">{t("knowledge.panel.layers")}</h3>
        </div>
        <p className={styles.sectionIntroduction}>
          {t("knowledge.panel.layersIntro")}
        </p>
        <div className={styles.epistemicStack}>
          {epistemicLayers.map((layer) => {
            const cards = evidenceCards.filter((card) => card.knowledge_origin === layer.origin)
            return (
              <section key={layer.origin} className={styles.epistemicLayer} data-origin={layer.origin}>
                <div className={styles.epistemicHeading}>
                  <div>
                    <h4>{layer.label}</h4>
                    <p>{t(layer.descriptionKey)}</p>
                  </div>
                  <span>{cards.length.toString().padStart(2, "0")}</span>
                </div>
                {evidenceLoading ? (
                  <div className={styles.layerStatus}>{t("knowledge.panel.resolvingEvidence")}</div>
                ) : evidenceFailed ? (
                  <div className={styles.layerStatus}>{t("knowledge.panel.evidenceFailed")}</div>
                ) : cards.length > 0 ? (
                  <div className={styles.evidenceTraceList}>
                    {cards.map((card) => <EvidenceTrace key={card.id} card={card} />)}
                  </div>
                ) : (
                  <div className={styles.layerStatus}>{t("knowledge.panel.noEvidence")}</div>
                )}
              </section>
            )
          })}
        </div>
      </section>

      <section className={styles.artifactSection} aria-labelledby="artifact-paths-title">
        <div className={styles.artifactSectionTitle}>
          <NetworkIcon aria-hidden="true" />
          <h3 id="artifact-paths-title">{t("knowledge.panel.paths")}</h3>
        </div>
        {relationships.length > 0 ? (
          <ul className={styles.relationshipList}>
            {relationships.map((relationship) => {
              const outbound = relationship.source_id === node.entity.id
              const otherId = outbound ? relationship.target_id : relationship.source_id
              const other = nodeById.get(otherId)
              return (
                <li key={relationship.id}>
                  <RouteIcon aria-hidden="true" />
                  <div>
                    <span>{outbound
                      ? humanize(relationship.type)
                      : t("knowledge.panel.inverseRelation", { relation: humanize(relationship.type) })}</span>
                    <strong>{other?.entity.name ?? otherId}</strong>
                    {relationship.evidence ? <p>{relationship.evidence}</p> : null}
                  </div>
                  <em>{relationship.confidence == null ? t("knowledge.panel.unscored") : `${Math.round(relationship.confidence * 100)}%`}</em>
                </li>
              )
            })}
          </ul>
        ) : (
          <div className={styles.artifactBlank}>
            <CircleDashedIcon aria-hidden="true" />
            {t("knowledge.panel.noRelationship")}
          </div>
        )}
      </section>

      <div className={styles.artifactPair}>
        <section className={styles.artifactSection} aria-labelledby="artifact-techniques-title">
          <div className={styles.artifactSectionTitle}>
            <WrenchIcon aria-hidden="true" />
            <h3 id="artifact-techniques-title">{t("knowledge.panel.connectedTechniques")}</h3>
          </div>
          {connectedTechniques.length > 0 ? (
            <ul className={styles.compactList}>
              {connectedTechniques.map((technique) => <li key={technique.entity.id}>{technique.entity.name}</li>)}
            </ul>
          ) : (
            <p className={styles.notRecorded}>{t("knowledge.panel.noTechnique")}</p>
          )}
        </section>
        <section className={styles.artifactSection} aria-labelledby="artifact-history-title">
          <div className={styles.artifactSectionTitle}>
            <HistoryIcon aria-hidden="true" />
            <h3 id="artifact-history-title">{t("knowledge.panel.history")}</h3>
          </div>
          <p className={context ? undefined : styles.notRecorded}>
            {context ?? t("knowledge.panel.historyMissing")}
          </p>
        </section>
      </div>

      <section className={styles.artifactSection} aria-labelledby="artifact-index-title">
        <div className={styles.artifactSectionTitle}>
          <BrainCircuitIcon aria-hidden="true" />
          <h3 id="artifact-index-title">{t("knowledge.panel.ontologyLimitations")}</h3>
        </div>
        <dl className={styles.artifactLedger}>
          <div>
            <dt>Ontology</dt>
            <dd>{node.ontology_paths.length > 0 ? node.ontology_paths.join(" · ") : t("knowledge.panel.notRecorded")}</dd>
          </div>
          <div>
            <dt>Domains</dt>
            <dd>{node.domains.length > 0 ? node.domains.map(humanize).join(", ") : t("knowledge.panel.notRecorded")}</dd>
          </div>
          <div>
            <dt>Aliases</dt>
            <dd>{node.entity.aliases.length > 0 ? node.entity.aliases.join(", ") : t("knowledge.panel.noneRecorded")}</dd>
          </div>
          <div>
            <dt>Limitations</dt>
            <dd>
              {node.limitations.length > 0 ? (
                <ul>{node.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
              ) : t("knowledge.panel.notRecorded")}
            </dd>
          </div>
        </dl>
      </section>
    </article>
  )
}
