"use client"

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  useTransition,
  type ReactNode,
} from "react"

import { localeCookieName, type Locale } from "@/lib/i18n/config"
import { formatMessage, type MessageKey, type MessageValues } from "@/lib/i18n/messages"

interface LocaleContextValue {
  locale: Locale
  isSwitching: boolean
  setLocale: (locale: Locale) => void
  t: (key: MessageKey, values?: MessageValues) => string
}

const LocaleContext = createContext<LocaleContextValue | null>(null)

export function LocaleProvider({
  initialLocale,
  children,
}: {
  initialLocale: Locale
  children: ReactNode
}) {
  const [locale, setLocaleState] = useState(initialLocale)
  const [isSwitching, startTransition] = useTransition()

  const setLocale = useCallback((nextLocale: Locale) => {
    document.cookie = `${localeCookieName}=${nextLocale}; Path=/; Max-Age=31536000; SameSite=Lax`
    document.documentElement.lang = nextLocale
    startTransition(() => setLocaleState(nextLocale))
  }, [])

  const t = useCallback(
    (key: MessageKey, values?: MessageValues) => formatMessage(locale, key, values),
    [locale]
  )

  const value = useMemo(
    () => ({ locale, isSwitching, setLocale, t }),
    [isSwitching, locale, setLocale, t]
  )

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}

export function useLocale() {
  const context = useContext(LocaleContext)
  if (!context) throw new Error("useLocale must be used within LocaleProvider")
  return context
}
