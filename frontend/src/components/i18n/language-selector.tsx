"use client"

import { LanguagesIcon } from "lucide-react"
import { useState } from "react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Button } from "@/components/ui/button"
import type { Locale } from "@/lib/i18n/config"
import { cn } from "@/lib/utils"

const options: Array<{ locale: Locale; shortLabel: string }> = [
  { locale: "en-US", shortLabel: "EN" },
  { locale: "zh-CN", shortLabel: "中文" },
]

export function LanguageSelector({ compact = false }: { compact?: boolean }) {
  const { locale, isSwitching, setLocale, t } = useLocale()
  const [announcedLocale, setAnnouncedLocale] = useState<Locale | null>(null)

  return (
    <div
      className={cn(
        "border-primary/20 bg-primary/[0.045] shadow-[inset_0_1px_0_color-mix(in_oklab,var(--primary)_10%,transparent)]",
        compact ? "rounded-lg p-1" : "rounded-xl p-3"
      )}
    >
      {compact ? null : (
        <div className="mb-2.5 flex items-center gap-2 text-[0.64rem] font-semibold tracking-[0.16em] text-primary/80 uppercase">
          <LanguagesIcon className="size-3.5" aria-hidden="true" />
          <span>{t("language.control")}</span>
        </div>
      )}
      <div
        role="group"
        aria-label={t("language.groupLabel")}
        aria-busy={isSwitching}
        className="grid grid-cols-2 gap-1 rounded-lg border border-border/70 bg-background/45 p-1"
      >
        {options.map((option) => {
          const active = locale === option.locale
          const languageName =
            option.locale === "en-US" ? t("language.english") : t("language.chinese")
          return (
            <Button
              key={option.locale}
              type="button"
              variant="ghost"
              size="sm"
              aria-label={languageName}
              aria-pressed={active}
              disabled={isSwitching}
              onClick={() => {
                if (active) return
                setAnnouncedLocale(option.locale)
                setLocale(option.locale)
              }}
              className={cn(
                "min-h-9 rounded-md px-2 font-mono text-xs tracking-[0.08em]",
                active
                  ? "border border-primary/30 bg-primary/12 text-primary shadow-[0_0_18px_color-mix(in_oklab,var(--primary)_10%,transparent)]"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              {option.shortLabel}
            </Button>
          )
        })}
      </div>
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {announcedLocale
          ? t("language.changed", {
              language:
                announcedLocale === "en-US"
                  ? t("language.english")
                  : t("language.chinese"),
            })
          : ""}
      </p>
    </div>
  )
}
