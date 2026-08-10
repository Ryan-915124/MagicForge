"use client"

import { useRouter } from "next/navigation"
import { useState, type FormEvent } from "react"
import { KeyRoundIcon, ShieldCheckIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { useAuth } from "@/features/auth/auth-provider"
import { MagicForgeApiError } from "@/lib/api/client"

function loginErrorMessage(error: unknown, t: ReturnType<typeof useLocale>["t"]) {
  if (!(error instanceof MagicForgeApiError)) return t("auth.error.failed")
  if (error.code === "invalid_credentials") return t("auth.error.invalidCredentials")
  if (error.code === "origin_validation_failed") return t("auth.error.origin")
  if (
    error.code === "authentication_not_ready" ||
    error.code === "authentication_dependency_unavailable" ||
    error.code === "database_not_ready"
  ) {
    return t("auth.error.persistence")
  }
  if (error.code === "backend_unreachable") return t("auth.error.backend")
  return error.message || t("auth.error.failed")
}

export function LoginForm({ nextPath }: { nextPath: string }) {
  const router = useRouter()
  const { login } = useAuth()
  const { t } = useLocale()
  const [identifier, setIdentifier] = useState("")
  const [password, setPassword] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (isSubmitting) return

    setIsSubmitting(true)
    setFormError(null)
    try {
      await login(identifier.trim(), password)
      router.replace(nextPath)
      router.refresh()
    } catch (cause) {
      setFormError(loginErrorMessage(cause, t))
      requestAnimationFrame(() => document.getElementById("login-error")?.focus())
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <Card className="glass-panel w-full max-w-md border border-primary/20 bg-card/90 shadow-2xl">
      <CardHeader className="border-b border-border/70 pb-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="flex size-10 items-center justify-center rounded-full border border-primary/30 bg-primary/10 text-primary">
            <ShieldCheckIcon className="size-4" aria-hidden="true" />
          </span>
          <span className="font-mono text-[0.65rem] tracking-[0.18em] text-primary/75 uppercase">
            {t("auth.login.privateAccess")}
          </span>
        </div>
        <CardTitle>
          <h1 className="text-balance font-heading text-3xl font-medium tracking-tight">
            {t("auth.login.title")}
          </h1>
        </CardTitle>
        <CardDescription className="max-w-sm leading-relaxed">
          {t("auth.login.description")}
        </CardDescription>
      </CardHeader>

      <CardContent>
        <form onSubmit={handleSubmit} aria-describedby="login-session-note">
          <FieldGroup>
            <Field data-disabled={isSubmitting || undefined}>
              <FieldLabel htmlFor="login-identifier">{t("auth.login.identifier")}</FieldLabel>
              <Input
                id="login-identifier"
                name="identifier"
                type="text"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                disabled={isSubmitting}
                value={identifier}
                onChange={(event) => setIdentifier(event.target.value)}
                className="h-11"
              />
              <FieldDescription>{t("auth.login.identifierHint")}</FieldDescription>
            </Field>

            <Field data-disabled={isSubmitting || undefined}>
              <FieldLabel htmlFor="login-password">{t("auth.login.password")}</FieldLabel>
              <Input
                id="login-password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                disabled={isSubmitting}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="h-11"
              />
            </Field>

            {formError ? (
              <Alert id="login-error" variant="destructive" tabIndex={-1}>
                <KeyRoundIcon aria-hidden="true" />
                <AlertTitle>{t("auth.error.title")}</AlertTitle>
                <AlertDescription>{formError}</AlertDescription>
              </Alert>
            ) : null}

            <Button type="submit" size="lg" disabled={isSubmitting} className="min-h-11 w-full">
              {isSubmitting ? (
                <>
                  <Spinner data-icon="inline-start" />
                  {t("auth.login.opening")}
                </>
              ) : (
                <>
                  <KeyRoundIcon data-icon="inline-start" />
                  {t("auth.login.submit")}
                </>
              )}
            </Button>
          </FieldGroup>
        </form>
      </CardContent>

      <CardFooter>
        <p id="login-session-note" className="text-xs leading-relaxed text-muted-foreground">
          {t("auth.login.sessionNote")}
        </p>
      </CardFooter>
    </Card>
  )
}
