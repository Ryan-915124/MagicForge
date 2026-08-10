export const supportedLocales = ["en-US", "zh-CN"] as const

export type Locale = (typeof supportedLocales)[number]

export const defaultLocale: Locale = "zh-CN"
export const localeCookieName = "magicforge_locale"

export function isLocale(value: string | null | undefined): value is Locale {
  return supportedLocales.includes(value as Locale)
}
