"use client"

import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"
import { useState } from "react"
import { LogInIcon, LogOutIcon, UserRoundIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { useAuth } from "@/features/auth/auth-provider"

function loginHref(pathname: string) {
  return `/login?next=${encodeURIComponent(pathname || "/")}`
}

export function AccountControl() {
  const pathname = usePathname()
  const router = useRouter()
  const { actor, error, logout, status } = useAuth()
  const { t } = useLocale()
  const [isLoggingOut, setIsLoggingOut] = useState(false)

  if (status === "loading") {
    return (
      <Button variant="outline" className="w-full justify-start" disabled>
        <Spinner data-icon="inline-start" />
        {t("auth.account.checking")}
      </Button>
    )
  }

  if (!actor || status !== "authenticated") {
    return (
      <div className="flex flex-col gap-2">
        <Button
          variant="outline"
          className="w-full justify-start"
          render={<Link href={loginHref(pathname)} />}
          nativeButton={false}
        >
          <LogInIcon data-icon="inline-start" />
          {t("auth.account.signIn")}
        </Button>
        {status === "unavailable" && error ? (
          <p role="status" className="px-1 text-xs leading-relaxed text-destructive">
            {t("auth.account.unavailable")}
          </p>
        ) : null}
      </div>
    )
  }

  async function handleLogout() {
    if (isLoggingOut) return
    setIsLoggingOut(true)
    try {
      await logout()
      router.replace("/login")
      router.refresh()
    } catch {
      setIsLoggingOut(false)
    }
  }

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-primary/20 bg-primary/[0.045] p-3" aria-label={t("auth.account.label")}>
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-full border border-primary/25 bg-background/55 text-primary">
          <UserRoundIcon className="size-3.5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <strong className="block truncate text-sm font-medium text-foreground">
            {actor.username}
          </strong>
          <span className="block truncate font-mono text-[0.62rem] tracking-[0.08em] text-muted-foreground uppercase">
            {t("auth.account.sessionActive")}
          </span>
        </div>
      </div>

      <div className="flex flex-wrap gap-1" aria-label={t("auth.account.roles")}>
        {actor.roles.map((role) => (
          <Badge key={role} variant="outline" translate="no">
            {role}
          </Badge>
        ))}
      </div>

      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="w-full justify-start"
        disabled={isLoggingOut}
        onClick={handleLogout}
      >
        {isLoggingOut ? (
          <Spinner data-icon="inline-start" />
        ) : (
          <LogOutIcon data-icon="inline-start" />
        )}
        {isLoggingOut ? t("auth.account.signingOut") : t("auth.account.signOut")}
      </Button>

      {error ? (
        <p role="alert" className="text-xs leading-relaxed text-destructive">
          {t("auth.account.signOutFailed")}
        </p>
      ) : null}
    </section>
  )
}
