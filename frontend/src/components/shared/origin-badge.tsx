import { Badge } from "@/components/ui/badge"
import type { KnowledgeOrigin } from "@/lib/api/types"
import { cn } from "@/lib/utils"

const originLabels: Record<KnowledgeOrigin, string> = {
  scientific_evidence: "Scientific",
  expert_practice: "Expert practice",
  personal_interpretation: "Interpretation",
}

export function isKnowledgeOrigin(value: string): value is KnowledgeOrigin {
  return value in originLabels
}

export function OriginBadge({ origin }: { origin: string }) {
  const normalized = isKnowledgeOrigin(origin) ? origin : null
  return (
    <Badge
      variant="outline"
      className={cn(
        normalized === "scientific_evidence" &&
          "border-origin-scientific/30 bg-origin-scientific/8 text-origin-scientific",
        normalized === "expert_practice" &&
          "border-origin-practice/30 bg-origin-practice/8 text-origin-practice",
        normalized === "personal_interpretation" &&
          "border-origin-interpretation/30 bg-origin-interpretation/8 text-origin-interpretation"
      )}
    >
      {normalized ? originLabels[normalized] : origin || "Unclassified"}
    </Badge>
  )
}
