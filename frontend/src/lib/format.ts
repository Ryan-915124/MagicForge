import { defaultLocale, type Locale } from "@/lib/i18n/config"

export function humanize(value: string) {
  return value.replaceAll("_", " ").replaceAll("-", " ")
}

export function formatPercent(value: number, locale: Locale = defaultLocale) {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 0,
  }).format(value)
}

export function formatCount(value: number, locale: Locale = defaultLocale) {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: 0,
  }).format(value)
}

export function formatDate(value: string, locale: Locale = defaultLocale) {
  return new Intl.DateTimeFormat(locale, {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value))
}
