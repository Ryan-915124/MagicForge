"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import type { ReactNode } from "react"
import { useState } from "react"
import {
  BookOpenTextIcon,
  ChartNoAxesCombinedIcon,
  FlaskConicalIcon,
  MenuIcon,
  MessagesSquareIcon,
  NetworkIcon,
  ShieldCheckIcon,
  ShieldAlertIcon,
  SparklesIcon,
} from "lucide-react"

import { AccountControl } from "@/components/app-shell/account-control"
import { LanguageSelector } from "@/components/i18n/language-selector"
import { useLocale } from "@/components/i18n/locale-provider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useRuntimeStatus } from "@/features/runtime/runtime-status-provider"
import { useAuth } from "@/features/auth/auth-provider"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"

const navigation = [
  { href: "/", label: "Magic Chat", icon: MessagesSquareIcon },
  { href: "/evidence", label: "Evidence Browser", icon: BookOpenTextIcon },
  { href: "/knowledge", label: "Knowledge Explorer", icon: NetworkIcon },
  { href: "/dashboard", label: "Corpus Dashboard", icon: ChartNoAxesCombinedIcon },
  { href: "/research", label: "Research Console", icon: FlaskConicalIcon },
  { href: "/governance", label: "Production Governance", icon: ShieldCheckIcon },
] as const

type ModuleName = "chat" | "evidence" | "knowledge" | "dashboard" | "research" | "governance"

function moduleFromPath(pathname: string): ModuleName {
  if (pathname.startsWith("/evidence")) return "evidence"
  if (pathname.startsWith("/knowledge")) return "knowledge"
  if (pathname.startsWith("/dashboard")) return "dashboard"
  if (pathname.startsWith("/research")) return "research"
  if (pathname.startsWith("/governance")) return "governance"
  return "chat"
}

function Brand() {
  const { t } = useLocale()

  return (
    <Link href="/" className="group flex items-center gap-3" aria-label={t("shell.home")}>
      <span className="flex size-9 items-center justify-center rounded-xl border border-primary/25 bg-primary/10 text-primary shadow-[0_0_32px_color-mix(in_oklab,var(--primary)_16%,transparent)] transition-colors group-hover:bg-primary/15">
        <SparklesIcon className="size-4" aria-hidden="true" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-[0.7rem] font-semibold tracking-[0.26em] text-primary uppercase">
          <span translate="no">MagicForge</span>
        </span>
        <span className="block truncate text-sm font-medium text-foreground" translate="no">Intelligence v0.2</span>
      </span>
    </Link>
  )
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname()
  const { t } = useLocale()

  return (
    <nav aria-label={t("shell.navigation")} className="flex flex-col gap-1">
      {navigation.map((item) => {
        const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)
        const Icon = item.icon
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group relative flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/55 hover:text-sidebar-accent-foreground",
              active && "text-[var(--module-ink)]"
            )}
            style={
              active
                ? { background: "color-mix(in oklab, var(--module-accent) 13%, transparent)" }
                : undefined
            }
          >
            <Icon className={cn("size-4", active && "text-[var(--module-accent)]")} aria-hidden="true" />
            <span translate="no">{item.label}</span>
            {active && <span className="absolute inset-y-2 left-0 w-px bg-[var(--module-accent)]" aria-hidden="true" />}
          </Link>
        )
      })}
    </nav>
  )
}

function GovernanceStatus() {
  const { t } = useLocale()
  const { health, refresh, status } = useRuntimeStatus()
  const profile = health?.profile ?? health?.mode
  const corpus = health?.corpus_id ?? health?.collection
  const isDemo = profile === "demo"
  const isReadOnly = health?.read_only ?? isDemo

  return (
    <div className="rounded-xl border border-border/70 bg-muted/25 p-3">
      <div
        className="flex items-center justify-between gap-2"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <span className="text-xs font-medium text-foreground">{t("shell.runtime")}</span>
        <Badge
          variant="outline"
          className={cn(
            status === "ready"
              ? "border-status-success/30 text-status-success"
              : "border-status-warning/30 text-status-warning"
          )}
          translate="no"
        >
          {status === "loading"
            ? t("shell.runtimeChecking")
            : status === "unavailable"
              ? t("shell.runtimeUnavailable")
              : profile}
        </Badge>
      </div>
      <div className="mt-3 flex items-start gap-2 text-xs leading-relaxed text-muted-foreground">
        <ShieldAlertIcon className="mt-0.5 size-3.5 shrink-0 text-status-warning" aria-hidden="true" />
        <span>
          {status === "ready" ? (
            <>
              <span translate={corpus ? "no" : undefined}>
                {corpus || t("shell.runtimeUnknownCorpus")}
              </span>
              {" · "}
              <span>
                {isReadOnly
                  ? t("shell.runtimeReadOnly")
                  : t("shell.runtimeGoverned")}
              </span>
            </>
          ) : t("shell.runtimeNoFallback")}
        </span>
      </div>
      {status === "unavailable" ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="mt-2 w-full justify-start"
          onClick={() => void refresh()}
        >
          {t("shell.runtimeRetry")}
        </Button>
      ) : null}
      {isDemo && status === "ready" ? (
        <p className="mt-2 font-mono text-[0.62rem] tracking-[0.06em] text-muted-foreground uppercase">
          {t("shell.syntheticDemo")}
        </p>
      ) : null}
    </div>
  )
}

function ProductAccessBoundary({
  children,
  pathname,
}: {
  children: ReactNode
  pathname: string
}) {
  const {
    actor,
    refresh: refreshAuth,
    status: authStatus,
  } = useAuth()
  const {
    health,
    refresh,
    status: runtimeStatus,
  } = useRuntimeStatus()
  const { t } = useLocale()

  if (pathname.startsWith("/governance")) return children

  if (runtimeStatus === "loading") {
    return (
      <section className="mx-auto flex min-h-[70dvh] max-w-3xl items-center px-6 py-16" aria-busy="true">
        <div className="w-full rounded-2xl border border-border bg-card/70 p-8">
          <p className="text-sm text-muted-foreground" role="status">
            {t("shell.accessChecking")}
          </p>
        </div>
      </section>
    )
  }

  if (runtimeStatus === "unavailable") {
    return (
      <section
        className="mx-auto flex min-h-[70dvh] max-w-3xl items-center px-6 py-16"
        role="alert"
      >
        <div className="w-full rounded-2xl border border-destructive/30 bg-card/70 p-8">
          <ShieldAlertIcon className="mb-5 size-6 text-destructive" aria-hidden="true" />
          <h1 className="font-serif text-3xl text-foreground">{t("shell.accessRuntimeUnavailable")}</h1>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            {t("shell.runtimeNoFallback")}
          </p>
          <Button type="button" className="mt-6" onClick={() => void refresh()}>
            {t("shell.runtimeRetry")}
          </Button>
        </div>
      </section>
    )
  }

  const profile = health?.profile ?? health?.mode
  const demoReadOnly = profile === "demo" && health?.read_only !== false
  const researchAllowed = Boolean(
    actor?.roles.some((role) => role === "operator" || role === "admin")
  )

  if (demoReadOnly) return children

  if (authStatus === "loading") {
    return (
      <section className="mx-auto flex min-h-[70dvh] max-w-3xl items-center px-6 py-16" aria-busy="true">
        <div className="w-full rounded-2xl border border-border bg-card/70 p-8">
          <p className="text-sm text-muted-foreground" role="status">
            {t("shell.accessSessionChecking")}
          </p>
        </div>
      </section>
    )
  }

  if (authStatus === "unavailable") {
    return (
      <section
        className="mx-auto flex min-h-[70dvh] max-w-3xl items-center px-6 py-16"
        role="alert"
      >
        <div className="w-full rounded-2xl border border-destructive/30 bg-card/70 p-8">
          <ShieldAlertIcon className="mb-5 size-6 text-destructive" aria-hidden="true" />
          <h1 className="font-serif text-3xl text-foreground">
            {t("shell.accessSessionUnavailable")}
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            {t("shell.accessSessionUnavailableDescription")}
          </p>
          <Button type="button" className="mt-6" onClick={() => void refreshAuth()}>
            {t("shell.accessSessionRetry")}
          </Button>
        </div>
      </section>
    )
  }

  if (authStatus !== "authenticated" || !actor) {
    return (
      <section className="mx-auto flex min-h-[70dvh] max-w-3xl items-center px-6 py-16">
        <div className="w-full rounded-2xl border border-border bg-card/70 p-8">
          <ShieldCheckIcon className="mb-5 size-6 text-primary" aria-hidden="true" />
          <h1 className="font-serif text-3xl text-foreground">{t("shell.accessSignInTitle")}</h1>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            {t("shell.accessSignInDescription")}
          </p>
          <Button
            className="mt-6"
            render={<Link href={`/login?next=${encodeURIComponent(pathname || "/")}`} />}
            nativeButton={false}
          >
            {t("auth.account.signIn")}
          </Button>
        </div>
      </section>
    )
  }

  if (pathname.startsWith("/research") && !researchAllowed) {
    return (
      <section className="mx-auto flex min-h-[70dvh] max-w-3xl items-center px-6 py-16">
        <div className="w-full rounded-2xl border border-border bg-card/70 p-8">
          <FlaskConicalIcon className="mb-5 size-6 text-primary" aria-hidden="true" />
          <h1 className="font-serif text-3xl text-foreground">{t("shell.accessResearchTitle")}</h1>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            {t("shell.accessResearchDescription")}
          </p>
        </div>
      </section>
    )
  }

  return children
}

export function AppShell({ children }: { children: ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const pathname = usePathname()
  const moduleName = moduleFromPath(pathname)
  const { t } = useLocale()

  if (pathname === "/login") {
    return (
      <div className="min-h-dvh" data-module="auth">
        <a
          href="#main-content"
          className="fixed top-3 left-3 z-50 -translate-y-20 rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-transform focus:translate-y-0"
        >
          {t("shell.skipToContent")}
        </a>
        {children}
      </div>
    )
  }

  return (
    <div className="min-h-dvh" data-module={moduleName}>
      <a
        href="#main-content"
        className="fixed top-3 left-3 z-50 -translate-y-20 rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-transform focus:translate-y-0"
      >
        {t("shell.skipToContent")}
      </a>
      <aside className="fixed inset-y-0 left-0 hidden w-64 border-r border-sidebar-border bg-sidebar shadow-[1rem_0_4rem_color-mix(in_oklab,var(--background)_52%,transparent)] lg:flex lg:flex-col">
        <div className="border-b border-sidebar-border p-5">
          <Brand />
        </div>
        <div className="flex min-h-0 flex-1 flex-col justify-between gap-8 p-4">
          <Navigation />
          <div className="flex flex-col gap-3">
            <AccountControl />
            <LanguageSelector />
            <GovernanceStatus />
          </div>
        </div>
      </aside>

      <header className="sticky top-0 flex h-16 items-center justify-between border-b border-border bg-background/88 px-4 backdrop-blur-xl lg:hidden">
        <Brand />
        <div className="flex items-center gap-2">
          <LanguageSelector compact />
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={t("shell.openNavigation")}
            onClick={() => setMobileOpen(true)}
          >
            <MenuIcon aria-hidden="true" />
          </Button>
        </div>
      </header>

      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-[min(21rem,88vw)] bg-sidebar">
          <SheetHeader>
            <SheetTitle translate="no">MagicForge</SheetTitle>
            <SheetDescription>{t("shell.mobileDescription")}</SheetDescription>
          </SheetHeader>
          <div className="flex flex-1 flex-col justify-between gap-8 px-4 pb-4">
            <Navigation onNavigate={() => setMobileOpen(false)} />
            <div className="flex flex-col gap-3">
              <AccountControl />
              <LanguageSelector />
              <GovernanceStatus />
            </div>
          </div>
        </SheetContent>
      </Sheet>

      <main id="main-content" tabIndex={-1} className="min-h-dvh lg:pl-64">
        <ProductAccessBoundary pathname={pathname}>
          {children}
        </ProductAccessBoundary>
      </main>
    </div>
  )
}
