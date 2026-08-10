"use client"

import { ExternalLinkIcon, FingerprintIcon, LockKeyholeIcon, ScanLineIcon } from "lucide-react"

import {
  clampUnitScore,
  evidenceLocators,
  safeSourceUrl,
} from "@/components/evidence/evidence-display"
import { useLocale } from "@/components/i18n/locale-provider"
import { Button } from "@/components/ui/button"
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress"
import type { EvidenceCard as EvidenceCardData } from "@/lib/api/types"
import { humanize } from "@/lib/format"
import type { MessageKey } from "@/lib/i18n/messages"

const locatorLabelKeys = {
  Page: "evidence.citation.locatorPage",
  "Printed page": "evidence.citation.locatorPrintedPage",
  Section: "evidence.citation.locatorSection",
  Paragraph: "evidence.citation.locatorParagraph",
  "Figure / table": "evidence.citation.locatorFigureTable",
  Timestamp: "evidence.citation.locatorTimestamp",
  "Registry locator": "evidence.citation.locatorRegistry",
  Media: "evidence.citation.locatorMedia",
} as const satisfies Record<string, MessageKey>

function RegistryValue({ children }: { children: React.ReactNode }) {
  return <dd className="evidence-registry-value" translate="no">{children}</dd>
}

interface CitationPanelProps {
  card: EvidenceCardData
  sealed?: boolean
}

export function CitationPanel({ card, sealed = false }: CitationPanelProps) {
  const { t } = useLocale()
  const sourceUrl = sealed ? null : safeSourceUrl(card.locator.source_locator)
  const locators = evidenceLocators(card)
  const assessment = card.confidence
  const extractionConfidence = clampUnitScore(card.extraction_confidence)

  return (
    <aside className="evidence-citation-case" aria-labelledby={`citation-case-${card.id}`}>
      <div className="evidence-citation-case-header">
        <div>
          <span>{t("evidence.citation.provenance")}</span>
          <h3 id={`citation-case-${card.id}`}>{t("evidence.citation.title")}</h3>
        </div>
        <FingerprintIcon aria-hidden="true" />
      </div>

      <dl className="evidence-source-register">
        <div>
          <dt>{t("evidence.citation.sourceType")}</dt>
          <RegistryValue>{humanize(card.source.source_type)}</RegistryValue>
        </div>
        <div>
          <dt>{t("evidence.citation.sourceYear")}</dt>
          <RegistryValue>{card.source.source_year ?? t("evidence.dossier.notRecorded")}</RegistryValue>
        </div>
        <div>
          <dt>{t("evidence.citation.peerReview")}</dt>
          <RegistryValue>{humanize(card.source.peer_review_status)}</RegistryValue>
        </div>
        <div>
          <dt>{t("evidence.citation.citationState")}</dt>
          <RegistryValue>{humanize(card.source.citation_status)}</RegistryValue>
        </div>
      </dl>

      <section className="evidence-locator-index" aria-labelledby={`locator-${card.id}`}>
        <h4 id={`locator-${card.id}`}>
          <ScanLineIcon aria-hidden="true" /> {t("evidence.citation.verifiableLocator")}
        </h4>
        {locators.length > 0 ? (
          <dl>
            {locators.map((locator, index) => (
              <div key={`${locator.label}-${index}`}>
                <dt>
                  {t(locatorLabelKeys[locator.label as keyof typeof locatorLabelKeys])}
                </dt>
                <dd translate="no">{locator.value}</dd>
              </div>
            ))}
          </dl>
        ) : <p>{t("evidence.citation.notRecordedCard")}</p>}
      </section>

      <section className="evidence-confidence-register" aria-labelledby={`assessment-${card.id}`}>
        <h4 id={`assessment-${card.id}`}>{t("evidence.citation.assessmentInstruments")}</h4>
        {assessment ? (
          <Progress value={clampUnitScore(assessment.score) * 100} className="evidence-assessment-progress">
            <ProgressLabel>
              {t("evidence.citation.evidenceAssessment")} ·{" "}
              <span translate="no">{humanize(assessment.label)}</span>
            </ProgressLabel>
            <ProgressValue />
          </Progress>
        ) : (
          <div className="evidence-assessment-missing">
            <span>{t("evidence.citation.evidenceAssessment")}</span>
            <strong>{t("evidence.citation.notAssessed")}</strong>
          </div>
        )}
        <Progress value={extractionConfidence * 100} className="evidence-extraction-progress">
          <ProgressLabel>{t("evidence.citation.extractionConfidence")}</ProgressLabel>
          <ProgressValue />
        </Progress>
        <p>{t("evidence.citation.confidenceNote")}</p>
      </section>

      <dl className="evidence-chain-register">
        <div><dt>{t("evidence.citation.sourceId")}</dt><RegistryValue>{card.source.source_id}</RegistryValue></div>
        <div><dt>{t("evidence.citation.citationId")}</dt><RegistryValue>{card.source.citation_id}</RegistryValue></div>
        <div><dt>{t("evidence.citation.documentId")}</dt><RegistryValue>{card.source.document_id}</RegistryValue></div>
      </dl>

      {sealed ? (
        <div className="evidence-source-sealed">
          <LockKeyholeIcon aria-hidden="true" /> {t("evidence.citation.externalSealed")}
        </div>
      ) : sourceUrl ? (
        <Button
          nativeButton={false}
          variant="outline"
          className="evidence-open-source"
          render={<a href={sourceUrl} target="_blank" rel="noopener noreferrer" />}
          aria-label={t("evidence.citation.openSourceAria")}
        >
          {t("evidence.citation.openSource")}
          <ExternalLinkIcon data-icon="inline-end" aria-hidden="true" />
        </Button>
      ) : (
        <p className="evidence-no-source-link">{t("evidence.citation.noSourceLink")}</p>
      )}
    </aside>
  )
}
