import type { ReactNode } from "react"

import { Badge } from "@/components/ui/badge"

interface PageHeaderProps {
  eyebrow: string
  title: string
  description: string
  module: "chat" | "evidence" | "knowledge" | "dashboard" | "research"
  mark: string
  instrument: string
  action?: ReactNode
}

export function PageHeader({ eyebrow, title, description, module, mark, instrument, action }: PageHeaderProps) {
  return (
    <header
      className={`module-header module-header-${module} flex min-h-44 flex-col justify-end gap-5 px-5 py-7 sm:px-7 lg:flex-row lg:items-end lg:justify-between lg:px-9`}
      data-mark={mark}
    >
      <div className="relative z-10 max-w-3xl">
        <div className="mb-3 flex items-center gap-2">
          <Badge variant="outline" className="module-kicker">
            {eyebrow}
          </Badge>
          <span className="font-mono text-[0.68rem] tracking-[0.16em] text-muted-foreground uppercase">
            Bootstrap / Unverified
          </span>
        </div>
        <h1 className="module-title text-balance text-3xl font-medium tracking-[-0.035em] sm:text-4xl">
          {title}
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">{description}</p>
      </div>
      <div className="relative z-10 flex shrink-0 items-center gap-3">
        <span className="font-mono text-[0.62rem] tracking-[0.18em] text-[var(--module-accent)] uppercase">
          {instrument}
        </span>
        {action}
      </div>
    </header>
  )
}
