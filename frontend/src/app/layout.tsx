import type { Metadata, Viewport } from "next"
import { cookies } from "next/headers"
import type { ReactNode } from "react"

import { AppShell } from "@/components/app-shell/app-shell"
import { LocaleProvider } from "@/components/i18n/locale-provider"
import { TooltipProvider } from "@/components/ui/tooltip"
import { AuthProvider } from "@/features/auth/auth-provider"
import { RuntimeStatusProvider } from "@/features/runtime/runtime-status-provider"
import { defaultLocale, isLocale, localeCookieName } from "@/lib/i18n/config"

import "./globals.css"

export const metadata: Metadata = {
  title: "MagicForge Intelligence",
  description: "Evidence-aware intelligence for the craft and cognition of magic.",
}

export const viewport: Viewport = {
  themeColor: "#0d0d14",
}

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  const localePreference = (await cookies()).get(localeCookieName)?.value
  const initialLocale = isLocale(localePreference) ? localePreference : defaultLocale

  return (
    <html lang={initialLocale} className="dark">
      <body>
        <LocaleProvider initialLocale={initialLocale}>
          <AuthProvider>
            <RuntimeStatusProvider>
              <TooltipProvider>
                <AppShell>{children}</AppShell>
              </TooltipProvider>
            </RuntimeStatusProvider>
          </AuthProvider>
        </LocaleProvider>
      </body>
    </html>
  )
}
