import type { LucideIcon } from "lucide-react"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

interface MetricCardProps {
  label: string
  value: number | string
  note: string
  icon: LucideIcon
}

export function MetricCard({ label, value, note, icon: Icon }: MetricCardProps) {
  return (
    <Card className="glass-panel overflow-hidden">
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardDescription>{label}</CardDescription>
          <CardTitle className="mt-2 font-mono text-3xl font-medium tracking-[-0.05em]">{value}</CardTitle>
        </div>
        <span className="flex size-9 items-center justify-center rounded-lg border border-primary/20 bg-primary/8 text-primary">
          <Icon className="size-4" aria-hidden="true" />
        </span>
      </CardHeader>
      <CardContent>
        <p className="text-xs leading-5 text-muted-foreground">{note}</p>
      </CardContent>
    </Card>
  )
}
