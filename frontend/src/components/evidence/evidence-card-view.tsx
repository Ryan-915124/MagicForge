"use client"

import {
  AlertTriangleIcon,
  BookMarkedIcon,
  BrainCircuitIcon,
  LockKeyholeIcon,
  ScrollTextIcon,
  ShieldCheckIcon,
} from "lucide-react"

import { CitationPanel } from "@/components/evidence/citation-panel"
import {
  isRestrictedEvidence,
  originPresentation,
  shortEvidenceId,
} from "@/components/evidence/evidence-display"
import { useLocale } from "@/components/i18n/locale-provider"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import type { EvidenceCard as EvidenceCardData } from "@/lib/api/types"
import { humanize } from "@/lib/format"

function RecordedValue({ value, empty }: { value: string | null | undefined; empty: string }) {
  const recorded = value?.trim()
  return recorded ? <dd translate="no">{recorded}</dd> : <dd>{empty}</dd>
}

function ConceptList({ values, empty }: { values: string[]; empty: string }) {
  if (values.length === 0) return <p className="evidence-index-empty">{empty}</p>
  return (
    <ul>
      {values.map((value) => <li key={value} translate="no">{value}</li>)}
    </ul>
  )
}

interface EvidenceDossierProps {
  card: EvidenceCardData
  surface?: "desk" | "sheet"
}

export function EvidenceDossier({ card, surface = "desk" }: EvidenceDossierProps) {
  const { t } = useLocale()
  const origin = originPresentation[card.knowledge_origin]
  const sealed = isRestrictedEvidence(card)
  const dossierTitleId = `dossier-title-${surface}-${card.id}`

  return (
    <article
      className="evidence-dossier"
      data-origin={card.knowledge_origin}
      data-surface={surface}
      aria-labelledby={dossierTitleId}
    >
      <div className="evidence-dossier-folder">
        <div className="evidence-dossier-tab">
          <span>{origin.code}</span>
          <strong translate="no">{origin.label}</strong>
          <small translate="no">{shortEvidenceId(card.id)}</small>
        </div>
        <div className="evidence-dossier-identity">
          <span>{t("evidence.dossier.accession")}</span>
          <strong translate="no">{shortEvidenceId(card.id)}</strong>
          <small translate="no">
            {t("evidence.dossier.schemaRevision", {
              schema: card.schema_version,
              revision: card.version,
            })}
          </small>
        </div>
        <div className="evidence-dossier-review-stamp" data-approved={card.review.approved}>
          {card.review.approved
            ? t("evidence.dossier.approved")
            : t("evidence.dossier.unverified")}
        </div>
      </div>

      <div className="evidence-extraction-rail" aria-hidden="true">
        <span /><i /><span />
      </div>

      <Card className="evidence-dossier-sheet">
        <CardContent className="evidence-dossier-sheet-content">
          <header className="evidence-claim-record">
            <div className="evidence-claim-record-meta">
              <Badge variant="outline" translate="no">{humanize(card.evidence_class)}</Badge>
              <Badge variant="outline" translate="no">{humanize(card.claim_role)}</Badge>
              <Badge variant="outline" translate="no">{humanize(card.evidence_level)}</Badge>
              <span translate="no">{humanize(card.claim_polarity)}</span>
            </div>
            <p>{t("evidence.dossier.atomicClaim")}</p>
            <h2 id={dossierTitleId} translate={sealed ? undefined : "no"}>
              {sealed ? t("evidence.dossier.restrictedClaim") : card.claim}
            </h2>
          </header>

          {sealed ? (
            <section className="evidence-sealed-record" aria-label={t("evidence.dossier.restrictedAria")}>
              <LockKeyholeIcon aria-hidden="true" />
              <div>
                <h3>{t("evidence.dossier.restrictedTitle")}</h3>
                <p>{t("evidence.dossier.restrictedDescription")}</p>
              </div>
            </section>
          ) : (
            <div className="evidence-dossier-body">
              <div className="evidence-dossier-reading-column">
                <section className="evidence-excerpt-sheet" aria-labelledby={`excerpt-${surface}-${card.id}`}>
                  <div className="evidence-section-label">
                    <ScrollTextIcon aria-hidden="true" />
                    <h3 id={`excerpt-${surface}-${card.id}`}>{t("evidence.dossier.supportedExcerpt")}</h3>
                  </div>
                  {card.evidence_excerpt ? (
                    <blockquote translate="no">{card.evidence_excerpt}</blockquote>
                  ) : (
                    <blockquote>{t("evidence.dossier.noExcerpt")}</blockquote>
                  )}
                  <span className="evidence-excerpt-hash" translate="no">HASH / {card.excerpt_hash}</span>
                </section>

                <section className="evidence-application-note" aria-labelledby={`application-${surface}-${card.id}`}>
                  <div className="evidence-section-label">
                    <BrainCircuitIcon aria-hidden="true" />
                    <h3 id={`application-${surface}-${card.id}`}>{t("evidence.dossier.magicApplication")}</h3>
                  </div>
                  {card.magic_application ? (
                    <p translate="no">{card.magic_application}</p>
                  ) : (
                    <p>{t("evidence.dossier.noMagicApplication")}</p>
                  )}
                  <span translate="no">{humanize(card.application_origin)}</span>
                </section>

                <section className="evidence-context-register" aria-labelledby={`context-${surface}-${card.id}`}>
                  <div className="evidence-section-label">
                    <BookMarkedIcon aria-hidden="true" />
                    <h3 id={`context-${surface}-${card.id}`}>{t("evidence.dossier.context")}</h3>
                  </div>
                  <dl>
                    <div>
                      <dt>{t("evidence.dossier.population")}</dt>
                      <RecordedValue value={card.population_context} empty={t("evidence.dossier.notRecorded")} />
                    </div>
                    <div>
                      <dt>{t("evidence.dossier.performance")}</dt>
                      <RecordedValue value={card.performance_context} empty={t("evidence.dossier.notRecorded")} />
                    </div>
                    <div>
                      <dt>{t("evidence.dossier.domains")}</dt>
                      {card.applicable_domain.length ? (
                        <dd translate="no">{card.applicable_domain.join(" · ")}</dd>
                      ) : (
                        <dd>{t("evidence.dossier.notRecorded")}</dd>
                      )}
                    </div>
                  </dl>
                </section>

                <section className="evidence-limitations-slip" aria-labelledby={`limitations-${surface}-${card.id}`}>
                  <div className="evidence-section-label">
                    <AlertTriangleIcon aria-hidden="true" />
                    <h3 id={`limitations-${surface}-${card.id}`}>{t("evidence.dossier.limitations")}</h3>
                  </div>
                  {card.limitations.length > 0 ? (
                    <ul>
                      {card.limitations.map((limitation) => (
                        <li key={limitation} translate="no">{limitation}</li>
                      ))}
                    </ul>
                  ) : <p>{t("evidence.dossier.noLimitations")}</p>}
                </section>
              </div>

              <CitationPanel card={card} />
            </div>
          )}

          {sealed && <CitationPanel card={card} sealed />}

          {!sealed ? <section className="evidence-cross-reference" aria-labelledby={`cross-reference-${surface}-${card.id}`}>
            <div className="evidence-cross-reference-heading">
              <div>
                <span>{t("evidence.dossier.crossReference")}</span>
                <h3 id={`cross-reference-${surface}-${card.id}`}>{t("evidence.dossier.relatedConcepts")}</h3>
              </div>
              <span>
                {t("evidence.dossier.entries", {
                  count: card.topic_tags.length + card.ontology_paths.length + card.mechanism_ids.length + card.principle_ids.length,
                })}
              </span>
            </div>
            <div className="evidence-cross-reference-grid">
              <div>
                <h4>{t("evidence.dossier.topics")}</h4>
                <ConceptList values={card.topic_tags} empty={t("evidence.dossier.noTopicTags")} />
              </div>
              <div>
                <h4>{t("evidence.dossier.ontology")}</h4>
                <ConceptList values={card.ontology_paths} empty={t("evidence.dossier.noOntologyPath")} />
              </div>
              <div>
                <h4>{t("evidence.dossier.mechanismIds")}</h4>
                <ConceptList values={card.mechanism_ids} empty={t("evidence.dossier.noMechanismLinks")} />
              </div>
              <div>
                <h4>{t("evidence.dossier.principleIds")}</h4>
                <ConceptList values={card.principle_ids} empty={t("evidence.dossier.noPrincipleLinks")} />
              </div>
            </div>
          </section> : null}

          <footer className="evidence-governance-strip">
            <div>
              <ShieldCheckIcon aria-hidden="true" />
              <span>{t("evidence.dossier.review")}</span>
              <strong translate="no">{humanize(card.review.review_status)}</strong>
            </div>
            <div>
              <span>{t("evidence.dossier.contradiction")}</span>
              <strong translate="no">{humanize(card.contradiction_status)}</strong>
            </div>
            <div>
              <span>{t("evidence.dossier.storage")}</span>
              <strong translate="no">{humanize(card.review.storage_permission)}</strong>
            </div>
            <div>
              <span>{t("evidence.dossier.sensitivity")}</span>
              <strong translate="no">{humanize(card.review.sensitive_information_level)}</strong>
            </div>
            <div>
              <span>{t("evidence.dossier.canonicalClaim")}</span>
              <strong translate="no">{card.canonical_claim_id}</strong>
            </div>
          </footer>
        </CardContent>
      </Card>
    </article>
  )
}

export const EvidenceCardView = EvidenceDossier
