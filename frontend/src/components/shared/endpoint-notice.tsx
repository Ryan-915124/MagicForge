"use client"

import { CableIcon, CircleAlertIcon, ConstructionIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import type { MagicForgeApiError } from "@/lib/api/client"

interface EndpointNoticeProps {
  error?: MagicForgeApiError | null
  route: string
  compact?: boolean
}

export function EndpointNotice({ error, route, compact = false }: EndpointNoticeProps) {
  const { t } = useLocale()
  if (!error) return null

  const unavailable = error.code === "endpoint_unavailable"
  const alphaUnavailable = error.code === "alpha_feature_unavailable"
  const Icon = alphaUnavailable
    ? ConstructionIcon
    : unavailable
      ? CableIcon
      : CircleAlertIcon

  return (
    <Alert className={compact ? "py-3" : undefined}>
      <Icon aria-hidden="true" />
      <AlertTitle className="flex flex-wrap items-center gap-2">
        {alphaUnavailable
          ? t("shared.alphaFeatureUnavailable")
          : unavailable
            ? t("shared.backendContractPending")
            : t("shared.backendUnavailable")}
        <Badge variant="outline" className="font-mono text-[0.65rem]" translate="no">
          {route}
        </Badge>
      </AlertTitle>
      <AlertDescription>{error.message}</AlertDescription>
    </Alert>
  )
}
