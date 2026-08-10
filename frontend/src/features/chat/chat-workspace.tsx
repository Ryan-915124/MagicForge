"use client"

import { useEffect, useRef } from "react"

import { QuestionComposer } from "@/components/chat/question-composer"
import { InstrumentPrimer, RevealTurnCard } from "@/components/chat/reveal-turn"
import { MotionPage } from "@/components/app-shell/motion-page"
import { useLocale } from "@/components/i18n/locale-provider"
import { useMagicChatSession } from "@/features/chat/use-magic-chat-session"

export function ChatWorkspace() {
  const { t } = useLocale()
  const { turns, isPending, liveMessage, placeQuestion, retryTurn } = useMagicChatSession()
  const latestTurnRef = useRef<HTMLDivElement>(null)
  const latestTurn = turns.at(-1)
  const latestTurnId = latestTurn?.id
  const latestTurnState = latestTurn?.state

  useEffect(() => {
    if (!latestTurnId) return
    const frame = window.requestAnimationFrame(() => {
      latestTurnRef.current?.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [latestTurnId, latestTurnState])

  return (
    <MotionPage className="chat-worktable min-h-[calc(100dvh-10rem)]">
      <div className="chat-table-light" aria-hidden="true" />
      <div className="chat-private-workspace">
        <QuestionComposer pending={isPending} onReveal={placeQuestion} />

        <section className="private-reveal-bench" aria-label={t("chat.workspace.revealBench")}>
          <div className="private-bench-register" aria-hidden="true">
            <span>{t("chat.workspace.revealDeck")}</span>
            <i />
            <b>{t("chat.workspace.caseCount", { count: String(turns.length).padStart(2, "0") })}</b>
          </div>

          {turns.length === 0 ? <InstrumentPrimer /> : null}
          <div className="reveal-turn-list">
            {turns.map((turn, index) => (
              <div
                key={turn.id}
                ref={index === turns.length - 1 ? latestTurnRef : undefined}
                className="reveal-turn-anchor"
              >
                <RevealTurnCard turn={turn} onRetry={retryTurn} />
              </div>
            ))}
          </div>
        </section>
      </div>
      <p className="sr-only" aria-live="polite" aria-atomic="true">{liveMessage}</p>
    </MotionPage>
  )
}
