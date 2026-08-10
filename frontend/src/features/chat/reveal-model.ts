import type { MagicForgeApiError } from "@/lib/api/client"
import type {
  GenerationResponse,
  MagicChatActKind,
  MagicChatAnswerAct,
  SourceSummary,
} from "@/lib/api/types"

const actOrder: Record<MagicChatActKind, number> = {
  effect: 1,
  hidden_structure: 2,
  cognitive_mechanism: 3,
}

export interface RevealDocument {
  rawContent: string
  structured: boolean
  formatVersion: string | null
  lead: string | null
  acts: MagicChatAnswerAct[]
  synthesis: string | null
}

interface RevealTurnBase {
  id: string
  number: number
  question: string
}

export type RevealTurn =
  | (RevealTurnBase & { state: "studying" })
  | (RevealTurnBase & {
      state: "revealed"
      document: RevealDocument
      sources: SourceSummary[]
    })
  | (RevealTurnBase & {
      state: "failed"
      error: MagicForgeApiError
    })

function isActKind(value: string): value is MagicChatActKind {
  return value in actOrder
}

function validatedActs(response: GenerationResponse): MagicChatAnswerAct[] | null {
  if (response.answer_format_version !== "magicforge.reveal.v1" || !response.acts?.length) {
    return null
  }

  const seen = new Set<MagicChatActKind>()
  let previousOrder = 0
  const acts: MagicChatAnswerAct[] = []

  for (const candidate of response.acts) {
    if (!isActKind(candidate.kind) || !candidate.content.trim()) return null
    const order = actOrder[candidate.kind]
    if (seen.has(candidate.kind) || order <= previousOrder) return null
    seen.add(candidate.kind)
    previousOrder = order
    acts.push({ kind: candidate.kind, content: candidate.content.trim() })
  }

  return acts.length > 0 ? acts : null
}

export function buildRevealDocument(response: GenerationResponse): RevealDocument {
  const acts = validatedActs(response)

  return {
    rawContent: response.result,
    structured: acts !== null,
    formatVersion: acts ? response.answer_format_version ?? null : null,
    lead: acts && response.lead?.trim() ? response.lead.trim() : null,
    acts: acts ?? [],
    synthesis: acts && response.synthesis?.trim() ? response.synthesis.trim() : null,
  }
}
