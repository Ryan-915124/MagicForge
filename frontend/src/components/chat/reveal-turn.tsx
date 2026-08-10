"use client"

import { memo, type ReactNode } from "react"
import {
  BrainCircuitIcon,
  DraftingCompassIcon,
  EyeIcon,
  FileWarningIcon,
  RotateCcwIcon,
  ScanLineIcon,
  type LucideIcon,
} from "lucide-react"

import { EvidencePanel, RelatedKnowledgePaths } from "@/components/chat/evidence-panel"
import { useLocale } from "@/components/i18n/locale-provider"
import { Badge } from "@/components/ui/badge"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Message, MessageContent } from "@/components/ui/message"
import type { RevealDocument, RevealTurn } from "@/features/chat/reveal-model"
import type { MagicChatActKind, SourceSummary } from "@/lib/api/types"
import type { MessageKey, MessageValues } from "@/lib/i18n/messages"

type Translator = (key: MessageKey, values?: MessageValues) => string

interface ActPresentation {
  roman: "I" | "II" | "III"
  titleKey: MessageKey
  registerKey: MessageKey
  materialKey: MessageKey
  icon: LucideIcon
}

const actPresentations: Record<MagicChatActKind, ActPresentation> = {
  effect: {
    roman: "I",
    titleKey: "chat.turn.effectTitle",
    registerKey: "chat.turn.effectRegister",
    materialKey: "chat.turn.effectMaterial",
    icon: EyeIcon,
  },
  hidden_structure: {
    roman: "II",
    titleKey: "chat.turn.structureTitle",
    registerKey: "chat.turn.structureRegister",
    materialKey: "chat.turn.structureMaterial",
    icon: DraftingCompassIcon,
  },
  cognitive_mechanism: {
    roman: "III",
    titleKey: "chat.turn.cognitionTitle",
    registerKey: "chat.turn.cognitionRegister",
    materialKey: "chat.turn.cognitionMaterial",
    icon: BrainCircuitIcon,
  },
}

function renderInline(text: string, sourceCount: number, t: Translator): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|Source\s+\d+)/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={`${part}-${index}`}>{part.slice(2, -2)}</strong>
    }

    const sourceMatch = /^Source\s+(\d+)$/.exec(part)
    if (sourceMatch) {
      const sourceNumber = Number(sourceMatch[1])
      return (
        <span
          key={`${part}-${index}`}
          className="source-reference-stamp"
          data-valid={sourceNumber > 0 && sourceNumber <= sourceCount}
          role="note"
          aria-label={
            sourceNumber > 0 && sourceNumber <= sourceCount
              ? t("chat.turn.sourceRetrieved", { number: sourceNumber })
              : t("chat.turn.sourceUnresolved", { number: sourceNumber })
          }
          title={
            sourceNumber > 0 && sourceNumber <= sourceCount
              ? t("chat.turn.retrievedRecord", { number: sourceNumber })
              : t("chat.turn.unresolvedReference")
          }
        >
          {part}
        </span>
      )
    }

    return part
  })
}

function RevealProse({ content, sourceCount, t }: { content: string; sourceCount: number; t: Translator }) {
  const blocks = content
    .replace(/^\[\[MAGICFORGE_(?:ACT:[A-Z_]+|SYNTHESIS)\]\]\s*$/gm, "")
    .trim()
    .split(/\n{2,}/)
    .filter(Boolean)

  return (
    <div className="reveal-prose">
      {blocks.map((block, blockIndex) => {
        const lines = block.split("\n").map((line) => line.trim()).filter(Boolean)
        const bulletItems = lines.map((line) => /^[-*]\s+(.+)$/.exec(line)?.[1])
        const orderedItems = lines.map((line) => /^\d+[.)]\s+(.+)$/.exec(line)?.[1])

        if (bulletItems.length > 0 && bulletItems.every(Boolean)) {
          return (
            <ul key={`bullet-${blockIndex}`}>
              {bulletItems.map((item, itemIndex) => (
                <li key={`${item}-${itemIndex}`}>{renderInline(item ?? "", sourceCount, t)}</li>
              ))}
            </ul>
          )
        }

        if (orderedItems.length > 0 && orderedItems.every(Boolean)) {
          return (
            <ol key={`ordered-${blockIndex}`}>
              {orderedItems.map((item, itemIndex) => (
                <li key={`${item}-${itemIndex}`}>{renderInline(item ?? "", sourceCount, t)}</li>
              ))}
            </ol>
          )
        }

        const heading = /^#{1,4}\s+(.+)$/.exec(lines[0] ?? "")
        if (heading) {
          return (
            <section key={`section-${blockIndex}`} className="reveal-prose-section">
              <h4>{renderInline(heading[1], sourceCount, t)}</h4>
              {lines.length > 1 ? (
                <p>{renderInline(lines.slice(1).join("\n"), sourceCount, t)}</p>
              ) : null}
            </section>
          )
        }

        return <p key={`paragraph-${blockIndex}`}>{renderInline(lines.join("\n"), sourceCount, t)}</p>
      })}
    </div>
  )
}

function EvidenceLedger({ sources, t }: { sources: SourceSummary[]; t: Translator }) {
  const counts = {
    scientific: 0,
    practice: 0,
    interpretation: 0,
  }

  for (const source of sources) {
    if (source.knowledge_origin === "scientific_evidence") counts.scientific += 1
    if (source.knowledge_origin === "expert_practice") counts.practice += 1
    if (source.knowledge_origin === "personal_interpretation") counts.interpretation += 1
  }

  return (
    <footer className="reveal-evidence-ledger" aria-label={t("chat.turn.evidenceComposition")}>
      <span>{t("chat.turn.attachedRegister")}</span>
      <div>
        <Badge variant="outline" data-origin="scientific_evidence">
          {t("chat.turn.science")} {counts.scientific}
        </Badge>
        <Badge variant="outline" data-origin="expert_practice">
          {t("chat.turn.practice")} {counts.practice}
        </Badge>
        <Badge variant="outline" data-origin="personal_interpretation">
          {t("chat.turn.interpretation")} {counts.interpretation}
        </Badge>
      </div>
      <small>{t("chat.turn.unverified")}</small>
    </footer>
  )
}

function StructuredReveal({
  document,
  sources,
  t,
}: {
  document: RevealDocument
  sources: SourceSummary[]
  t: Translator
}) {
  const defaultActs = document.acts.map((act) => act.kind)

  return (
    <>
      {document.lead ? (
        <aside className="reveal-opening-note">
          <h3>{t("chat.turn.openingObservation")}</h3>
          <RevealProse content={document.lead} sourceCount={sources.length} t={t} />
        </aside>
      ) : null}

      <Accordion multiple defaultValue={defaultActs} className="reveal-act-stack">
        {document.acts.map((act) => {
          const presentation = actPresentations[act.kind]
          const Icon = presentation.icon
          return (
            <AccordionItem key={act.kind} value={act.kind} className="reveal-act" data-act={act.kind}>
              <AccordionTrigger className="reveal-act-trigger transition-[background-color,color,box-shadow] duration-200">
                <span className="reveal-act-number">
                  {t("chat.turn.act", { number: presentation.roman })}
                </span>
                <span className="reveal-act-identity">
                  <Icon aria-hidden="true" />
                  <span>
                    <small>{t(presentation.registerKey)}</small>
                    <strong>{t(presentation.titleKey)}</strong>
                  </span>
                </span>
                <span className="reveal-act-material">{t(presentation.materialKey)}</span>
              </AccordionTrigger>
              <AccordionContent className="reveal-act-content">
                <RevealProse content={act.content} sourceCount={sources.length} t={t} />
              </AccordionContent>
            </AccordionItem>
          )
        })}
      </Accordion>

      {document.synthesis ? (
        <aside className="magician-synthesis-note">
          <span aria-hidden="true">MF</span>
          <div>
            <h3>{t("chat.turn.magicianSynthesis")}</h3>
            <RevealProse content={document.synthesis} sourceCount={sources.length} t={t} />
          </div>
        </aside>
      ) : null}
    </>
  )
}

function RevealDeck({ turn, t }: { turn: Extract<RevealTurn, { state: "revealed" }>; t: Translator }) {
  return (
    <article className="reveal-deck" aria-labelledby={`reveal-title-${turn.id}`}>
      <span className="reveal-deck-clamp" aria-hidden="true"><i /><i /></span>
      <header className="reveal-deck-register">
        <div>
          <span>{t("chat.turn.privateAnalysis", { number: String(turn.number).padStart(2, "0") })}</span>
          <h2 id={`reveal-title-${turn.id}`}>{t("chat.turn.revealTitle")}</h2>
        </div>
        <span className="reveal-deck-stamp" aria-label={t("chat.turn.revealDocument")}>MF<br />R-{String(turn.number).padStart(2, "0")}</span>
      </header>

      {turn.document.structured ? (
        <StructuredReveal document={turn.document} sources={turn.sources} t={t} />
      ) : (
        <section className="legacy-field-note" aria-label={t("chat.turn.unstructuredNote")}>
          <h3>{t("chat.turn.legacyNote")}</h3>
          <RevealProse content={turn.document.rawContent} sourceCount={turn.sources.length} t={t} />
        </section>
      )}

      {turn.sources.length > 0 ? <EvidenceLedger sources={turn.sources} t={t} /> : null}
    </article>
  )
}

function QuestionCard({ turn, t }: { turn: RevealTurn; t: Translator }) {
  return (
    <article className="question-playing-card question-playing-card-v03" aria-label={t("chat.turn.yourQuestion")}>
      <span className="question-card-corner-v03" aria-hidden="true">
        Q<b>♠</b>
      </span>
      <div>
        <p>{t("chat.turn.questionCard", { number: String(turn.number).padStart(2, "0") })}</p>
        <blockquote>{turn.question}</blockquote>
      </div>
      <span className="question-card-watermark" aria-hidden="true">♠</span>
    </article>
  )
}

function StudyingReveal({ turn, t }: { turn: Extract<RevealTurn, { state: "studying" }>; t: Translator }) {
  return (
    <article className="instrument-reading-board" aria-label={t("chat.turn.studyingLabel")}>
      <div className="instrument-reading-aperture" aria-hidden="true"><ScanLineIcon /></div>
      <div>
        <span>{t("chat.turn.instrumentReading", { number: String(turn.number).padStart(2, "0") })}</span>
        <h2 className="shimmer">{t("chat.turn.studyingTitle")}</h2>
        <p>{t("chat.turn.studyingDescription")}</p>
      </div>
      <div className="instrument-register-line" aria-hidden="true"><i /><i /><i /><i /></div>
    </article>
  )
}

function FailedReveal({ turn, onRetry, t }: {
  turn: Extract<RevealTurn, { state: "failed" }>
  onRetry: (turn: Extract<RevealTurn, { state: "failed" }>) => void
  t: Translator
}) {
  const timedOut = turn.error.code === "upstream_timeout"
  return (
    <article className="instrument-failure-board" role="alert">
      <FileWarningIcon aria-hidden="true" />
      <div>
        <span>{t("chat.turn.interrupted", { number: String(turn.number).padStart(2, "0") })}</span>
        <h2>{timedOut ? t("chat.turn.timeoutTitle") : t("chat.turn.failureTitle")}</h2>
        <p>{turn.error.message}</p>
      </div>
      <Button variant="outline" size="lg" className="instrument-retry-action" onClick={() => onRetry(turn)}>
        <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
        {t("chat.turn.retry")}
      </Button>
    </article>
  )
}

export function InstrumentPrimer() {
  const { t } = useLocale()

  return (
    <section className="instrument-primer" aria-labelledby="instrument-primer-title">
      <span className="instrument-primer-mark" aria-hidden="true">MF</span>
      <div>
        <span>{t("chat.turn.ready")}</span>
        <h2 id="instrument-primer-title">{t("chat.turn.clearTable")}</h2>
        <p>{t("chat.turn.primer")}</p>
      </div>
      <ol aria-label={t("chat.turn.revealSequence")}>
        <li><b>I</b> {t("chat.header.effect")}</li>
        <li><b>II</b> {t("chat.header.structure")}</li>
        <li><b>III</b> {t("chat.header.cognition")}</li>
        <li><b>IV</b> {t("chat.header.evidence")}</li>
      </ol>
    </section>
  )
}

export const RevealTurnCard = memo(function RevealTurnCard({ turn, onRetry }: {
  turn: RevealTurn
  onRetry: (turn: Extract<RevealTurn, { state: "failed" }>) => void
}) {
  const { t } = useLocale()

  return (
    <section className="reveal-turn" aria-label={t("chat.turn.questionReveal", { number: turn.number })}>
      <Message align="end" className="question-message-row">
        <MessageContent>
          <Bubble variant="ghost" align="end" className="question-message-surface">
            <BubbleContent className="question-message-content"><QuestionCard turn={turn} t={t} /></BubbleContent>
          </Bubble>
        </MessageContent>
      </Message>

      <Message align="start" className="answer-message-row">
        <MessageContent>
          <Bubble variant="ghost" className="answer-message-surface">
            <BubbleContent className="answer-message-content">
              {turn.state === "studying" ? <StudyingReveal turn={turn} t={t} /> : null}
              {turn.state === "failed" ? <FailedReveal turn={turn} onRetry={onRetry} t={t} /> : null}
              {turn.state === "revealed" ? (
                <>
                  <RevealDeck turn={turn} t={t} />
                  {turn.sources.length > 0 ? (
                    <div className="turn-research-attachments turn-research-attachments-v03">
                      <EvidencePanel sources={turn.sources} />
                      <RelatedKnowledgePaths question={turn.question} sources={turn.sources} />
                    </div>
                  ) : null}
                </>
              ) : null}
            </BubbleContent>
          </Bubble>
        </MessageContent>
      </Message>
    </section>
  )
})
