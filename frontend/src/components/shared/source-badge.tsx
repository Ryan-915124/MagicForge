import { Badge } from "@/components/ui/badge"
import { humanize } from "@/lib/format"

export function SourceBadge({ sourceType }: { sourceType: string }) {
  return <Badge variant="secondary">{humanize(sourceType)}</Badge>
}
