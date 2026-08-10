import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export function ConfidenceBadge({
  label,
  score,
  className,
}: {
  label?: string | null
  score?: number | null
  className?: string
}) {
  const value = label || (typeof score === "number" ? `${Math.round(score * 100)}% confidence` : "Unassessed")

  return (
    <Badge variant="outline" className={cn("font-mono text-[0.62rem]", className)}>
      {value}
    </Badge>
  )
}
