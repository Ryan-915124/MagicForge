import type { EvidenceCard, KnowledgeOrigin } from "@/lib/api/types"

export const originPresentation: Record<
  KnowledgeOrigin,
  { code: string; label: string }
> = {
  scientific_evidence: { code: "SCI", label: "Scientific evidence" },
  expert_practice: { code: "PRX", label: "Expert practice" },
  personal_interpretation: { code: "INT", label: "Interpretation" },
}

export function shortEvidenceId(id: string) {
  const compact = id.replace(/[^a-zA-Z0-9]/g, "")
  return `MF-E-${compact.slice(-8).toUpperCase()}`
}

export function clampUnitScore(value: number) {
  return Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0))
}

export function safeSourceUrl(locator: string | null | undefined) {
  if (!locator) return null

  try {
    const url = new URL(locator)
    if (!['http:', 'https:'].includes(url.protocol)) return null
    if (url.username || url.password) return null
    return url.toString()
  } catch {
    return null
  }
}

function formatTimestamp(value: number) {
  const seconds = Math.max(0, Math.floor(value))
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${minutes}:${String(remainder).padStart(2, "0")}`
}

export function evidenceLocators(card: EvidenceCard) {
  const { locator } = card
  const values: Array<{ label: string; value: string }> = []

  if (locator.page_number !== null) values.push({ label: "Page", value: String(locator.page_number) })
  if (locator.printed_page) values.push({ label: "Printed page", value: locator.printed_page })
  if (locator.section) values.push({ label: "Section", value: locator.section })
  if (locator.paragraph !== null) values.push({ label: "Paragraph", value: String(locator.paragraph) })
  if (locator.figure_or_table) values.push({ label: "Figure / table", value: locator.figure_or_table })
  if (locator.timestamp_start !== null) {
    const end = locator.timestamp_end !== null ? `–${formatTimestamp(locator.timestamp_end)}` : ""
    values.push({ label: "Timestamp", value: `${formatTimestamp(locator.timestamp_start)}${end}` })
  }
  if (locator.source_locator) values.push({ label: "Registry locator", value: locator.source_locator })
  if (values.length === 0 && locator.media_type) values.push({ label: "Media", value: locator.media_type })

  return values
}

export function isRestrictedEvidence(card: EvidenceCard) {
  return (
    card.review.sensitive_information_level === "restricted" ||
    card.review.sensitive_information_level === "secret_method" ||
    card.secret_exposure_level === "method_detail" ||
    card.secret_exposure_level === "operational_secret"
  )
}
