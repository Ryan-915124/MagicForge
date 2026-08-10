import Link from "next/link"
import { SparklesIcon } from "lucide-react"

import { LanguageSelector } from "@/components/i18n/language-selector"
import { LoginForm } from "@/features/auth/login-form"
import { safeNextPath } from "@/lib/auth/redirect"

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string | string[] }>
}) {
  const nextPath = safeNextPath((await searchParams).next)

  return (
    <div className="module-stage min-h-dvh overflow-hidden" data-module="auth">
      <div className="magic-grid pointer-events-none absolute inset-0 opacity-30" aria-hidden="true" />
      <div
        className="pointer-events-none absolute top-[-14rem] left-1/2 size-[34rem] -translate-x-1/2 rounded-full bg-primary/10 blur-3xl"
        aria-hidden="true"
      />

      <header className="relative flex min-h-20 items-center justify-between gap-4 border-b border-border/70 px-5 sm:px-8">
        <Link href="/" className="group flex items-center gap-3" aria-label="MagicForge home">
          <span className="flex size-9 items-center justify-center rounded-xl border border-primary/25 bg-primary/10 text-primary transition-colors group-hover:bg-primary/15">
            <SparklesIcon className="size-4" aria-hidden="true" />
          </span>
          <span>
            <strong className="block text-[0.7rem] tracking-[0.26em] text-primary uppercase" translate="no">
              MagicForge
            </strong>
            <span className="block text-sm text-foreground" translate="no">
              Intelligence v0.2
            </span>
          </span>
        </Link>
        <LanguageSelector compact />
      </header>

      <main id="main-content" className="relative grid min-h-[calc(100dvh-5rem)] place-items-center px-5 py-12 sm:px-8">
        <LoginForm nextPath={nextPath} />
      </main>
    </div>
  )
}
