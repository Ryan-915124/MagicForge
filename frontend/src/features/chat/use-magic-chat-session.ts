"use client"

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react"

import { useLocale } from "@/components/i18n/locale-provider"
import { MagicForgeApiError, magicForgeApi } from "@/lib/api/client"
import { buildRevealDocument, type RevealTurn } from "@/features/chat/reveal-model"

type TurnAction =
  | { type: "place"; turn: RevealTurn }
  | { type: "replace"; turn: RevealTurn }

function turnReducer(turns: RevealTurn[], action: TurnAction): RevealTurn[] {
  if (action.type === "place") return [...turns, action.turn]
  return turns.map((turn) => (turn.id === action.turn.id ? action.turn : turn))
}

function normalizeError(cause: unknown, fallbackMessage: string): MagicForgeApiError {
  if (cause instanceof MagicForgeApiError) return cause
  return new MagicForgeApiError(fallbackMessage, "backend_error", 500)
}

export function useMagicChatSession() {
  const { t } = useLocale()
  const [turns, dispatch] = useReducer(turnReducer, [])
  const pendingRef = useRef(false)
  const nextNumberRef = useRef(1)
  const translateRef = useRef(t)

  useEffect(() => {
    translateRef.current = t
  }, [t])

  const isPending = useMemo(
    () => turns.some((turn) => turn.state === "studying"),
    [turns]
  )
  const liveMessage = useMemo(() => {
    const latestTurn = turns.at(-1)
    if (!latestTurn) return t("chat.session.ready")
    if (latestTurn.state === "studying") {
      return t("chat.session.studying", { number: latestTurn.number })
    }
    if (latestTurn.state === "revealed") {
      return t("chat.session.completed", {
        number: latestTurn.number,
        count: latestTurn.sources.length,
      })
    }
    return t("chat.session.failed", { number: latestTurn.number })
  }, [t, turns])

  const executeReveal = useCallback(async (turn: RevealTurn) => {
    pendingRef.current = true

    try {
      const response = await magicForgeApi.chat(turn.question)
      dispatch({
        type: "replace",
        turn: {
          id: turn.id,
          number: turn.number,
          question: turn.question,
          state: "revealed",
          document: buildRevealDocument(response),
          sources: response.sources,
        },
      })
    } catch (cause) {
      dispatch({
        type: "replace",
        turn: {
          id: turn.id,
          number: turn.number,
          question: turn.question,
          state: "failed",
          error: normalizeError(cause, translateRef.current("chat.session.requestFailed")),
        },
      })
    } finally {
      pendingRef.current = false
    }
  }, [])

  const placeQuestion = useCallback(
    (question: string) => {
      const normalized = question.trim()
      if (!normalized || pendingRef.current) return false

      const turn: RevealTurn = {
        id: crypto.randomUUID(),
        number: nextNumberRef.current,
        question: normalized,
        state: "studying",
      }
      nextNumberRef.current += 1
      dispatch({ type: "place", turn })
      void executeReveal(turn)
      return true
    },
    [executeReveal]
  )

  const retryTurn = useCallback(
    (turn: Extract<RevealTurn, { state: "failed" }>) => {
      if (pendingRef.current) return
      const studyingTurn: RevealTurn = {
        id: turn.id,
        number: turn.number,
        question: turn.question,
        state: "studying",
      }
      dispatch({ type: "replace", turn: studyingTurn })
      void executeReveal(studyingTurn)
    },
    [executeReveal]
  )

  return { turns, isPending, liveMessage, placeQuestion, retryTurn }
}
