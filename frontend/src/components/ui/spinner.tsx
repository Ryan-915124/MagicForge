"use client"

import { useLocale } from "@/components/i18n/locale-provider"
import { cn } from "@/lib/utils"
import { Loader2Icon } from "lucide-react"

function Spinner({ className, ...props }: React.ComponentProps<"svg">) {
  const { t } = useLocale()

  return (
    <Loader2Icon data-slot="spinner" role="status" aria-label={t("shared.loading")} className={cn("size-4 animate-spin", className)} {...props} />
  )
}

export { Spinner }
