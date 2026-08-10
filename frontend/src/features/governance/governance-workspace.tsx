"use client"

import Link from "next/link"
import {
  CircleAlertIcon,
  DatabaseZapIcon,
  ExternalLinkIcon,
  FingerprintIcon,
  KeyRoundIcon,
  LockKeyholeIcon,
  RefreshCwIcon,
  ScanLineIcon,
  ServerCrashIcon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
} from "lucide-react"
import { useEffect, useState } from "react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useAuth } from "@/features/auth/auth-provider"

import { ReleaseDesk } from "./release-desk"
import { ReviewDesk } from "./review-desk"
import { MutationConfirmation } from "./governance-common"
import { updateGovernanceUrl, useGovernanceSearchParams } from "./governance-url-state"
import styles from "./governance.module.css"

function AccessState() {
  const {
    status,
    error,
    errorCode,
    isRefreshing,
    lastCheckedAt,
    refresh,
    verificationCount,
  } = useAuth()
  const { locale, t } = useLocale()

  if (status === "loading") {
    return (
      <div className={styles.authState} role="status" aria-live="polite" aria-busy="true">
        <span className="sr-only">{t("governance.loading.authentication")}</span>
        <Skeleton className="h-3 w-32" aria-hidden="true" />
        <Skeleton className="mt-4 h-10 w-3/4" aria-hidden="true" />
        <Skeleton className="mt-3 h-16 w-full" aria-hidden="true" />
      </div>
    )
  }
  if (status === "anonymous") {
    return (
      <div className={styles.authState}>
        <KeyRoundIcon className="mb-3 size-5 text-[var(--module-accent)]" aria-hidden="true" />
        <h1 className={styles.sectionTitle}>{t("governance.auth.required")}</h1>
        <p className={styles.sectionDescription}>{t("governance.auth.requiredDescription")}</p>
        <Button
          className="mt-5"
          render={<Link href="/login?next=/governance" />}
          nativeButton={false}
        >
          {t("governance.auth.signIn")}
        </Button>
      </div>
    )
  }
  if (status === "unavailable") {
    const backendUnavailable = errorCode === "backend_unreachable"
    const databaseUnavailable = [
      "database_not_ready",
      "authentication_not_ready",
      "authentication_dependency_unavailable",
    ].includes(errorCode ?? "")
    const requestTimedOut = errorCode === "upstream_timeout"
    const title = backendUnavailable
      ? t("governance.auth.backendUnavailable")
      : databaseUnavailable
        ? t("governance.auth.databaseUnavailable")
        : requestTimedOut
          ? t("governance.auth.verificationTimedOut")
          : t("governance.auth.unavailable")
    const description = backendUnavailable
      ? t("governance.auth.backendUnavailableDescription")
      : databaseUnavailable
        ? t("governance.auth.databaseUnavailableDescription")
        : requestTimedOut
          ? t("governance.auth.verificationTimedOutDescription")
          : error || t("governance.error.generic")
    const checkedAt = lastCheckedAt
      ? new Intl.DateTimeFormat(locale, {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }).format(lastCheckedAt)
      : null
    const IssueIcon = backendUnavailable
      ? ServerCrashIcon
      : databaseUnavailable
        ? DatabaseZapIcon
        : CircleAlertIcon

    return (
      <div
        className={styles.authState}
        aria-busy={isRefreshing}
        data-auth-error={errorCode ?? "unknown"}
      >
        <Alert variant="destructive" aria-live="polite">
          <IssueIcon aria-hidden="true" />
          <AlertTitle>{title}</AlertTitle>
          <AlertDescription>
            <p>{description}</p>
            {checkedAt ? (
              <p className={styles.authProbe}>
                {t("governance.auth.lastChecked", {
                  count: verificationCount,
                  time: checkedAt,
                })}
              </p>
            ) : null}
          </AlertDescription>
        </Alert>
        <div className={styles.authActions}>
          <Button
            variant="outline"
            disabled={isRefreshing}
            onClick={() => void refresh()}
          >
            {isRefreshing ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
            )}
            {isRefreshing
              ? t("governance.auth.retrying")
              : t("governance.action.retry")}
          </Button>
          <Button
            variant="ghost"
            render={(
              <Link
                href="/api/magicforge/health"
                target="_blank"
                rel="noreferrer"
              />
            )}
            nativeButton={false}
          >
            {t("governance.auth.openDiagnostic")}
            <ExternalLinkIcon data-icon="inline-end" aria-hidden="true" />
          </Button>
        </div>
      </div>
    )
  }
  return null
}

export function GovernanceWorkspace() {
  const { actor, status } = useAuth()
  const { t } = useLocale()
  const searchParams = useGovernanceSearchParams()
  const [releaseDirty, setReleaseDirty] = useState(false)
  const [releaseDraftRevision, setReleaseDraftRevision] = useState(0)
  const [pendingDesk, setPendingDesk] = useState<"review" | "release" | null>(null)

  const canReview = actor?.roles.includes("reviewer") ?? false
  const canOperate = actor?.roles.includes("operator") ?? false
  const initialDesk = canReview ? "review" : canOperate ? "release" : "none"
  const requestedDesk = searchParams.get("desk")
  const activeDesk = requestedDesk === "review" && canReview
    ? "review"
    : requestedDesk === "release" && canOperate
      ? "release"
      : initialDesk

  useEffect(() => {
    if (activeDesk !== "none" && requestedDesk !== activeDesk) {
      updateGovernanceUrl({ desk: activeDesk }, "replace")
    }
  }, [activeDesk, requestedDesk])

  if (status !== "authenticated" || !actor) {
    return <div className={styles.room}><AccessState /></div>
  }

  const selectDesk = (nextDesk: string) => {
    if (nextDesk !== "review" && nextDesk !== "release") return
    if (activeDesk === "release" && releaseDirty) {
      setPendingDesk(nextDesk)
      return
    }
    updateGovernanceUrl({ desk: nextDesk }, "push")
  }

  return (
    <div className={styles.room}>
      <div className={styles.shell}>
        <header className={styles.header}>
          <div className={styles.headerDatum} aria-hidden="true">
            <span>MF / HUMAN AUTHORITY</span>
            <i />
            <i />
            <i />
          </div>
          <div className={styles.headerCopy}>
            <p className={styles.eyebrow}>{t("governance.header.eyebrow")}</p>
            <h1 className={styles.title}>{t("governance.header.title")}</h1>
            <p className={styles.lede}>{t("governance.header.description")}</p>
          </div>
          <div className={styles.actorSeal} aria-label={t("governance.actor.label")}>
            <div className={styles.actorSealGlyph} aria-hidden="true">
              <FingerprintIcon />
              <span />
            </div>
            <div>
              <p className={styles.microLabel}>{t("governance.actor.authenticated")}</p>
              <p className={styles.actorName}>{actor.username}</p>
              <div className={styles.roleList}>
                {actor.roles.map((role) => <Badge key={role} variant="outline">{role}</Badge>)}
              </div>
            </div>
          </div>
        </header>

        <div className={styles.custodyRail} aria-label={t("governance.custody.label")}>
          {[
            ["01", t("governance.term.source"), t("governance.custody.source")],
            ["02", t("governance.term.claim"), t("governance.custody.claim")],
            ["03", t("governance.term.mapping"), t("governance.custody.mapping")],
            ["04", t("governance.term.manifest"), t("governance.custody.manifest")],
            ["05", t("governance.term.corpus"), t("governance.custody.corpus")],
          ].map(([number, name, note]) => (
            <div className={styles.custodyStep} key={name}>
              <span className={styles.custodyIndex}>{number}</span>
              <span className={styles.custodyBeam} aria-hidden="true"><i /></span>
              <span className={styles.custodyNote}>{note}</span>
              <strong translate="no">{name}</strong>
            </div>
          ))}
        </div>

        {!canReview && !canOperate ? (
          <Alert className={`${styles.notice} mt-4`}>
            <LockKeyholeIcon aria-hidden="true" />
            <AlertTitle>{t("governance.auth.noWorkspace")}</AlertTitle>
            <AlertDescription>{t("governance.auth.noWorkspaceDescription")}</AlertDescription>
          </Alert>
        ) : (
          <Tabs value={activeDesk} onValueChange={selectDesk} className={styles.workspace}>
            <div className={styles.deskTabs}>
              <div className={styles.instrumentLabel}>
                <ScanLineIcon aria-hidden="true" />
                <span>{t("governance.custody.label")}</span>
              </div>
              <TabsList variant="line">
                {canReview && (
                  <TabsTrigger value="review" className={styles.deskTab}>
                    <ShieldCheckIcon aria-hidden="true" /> {t("governance.desk.review")}
                  </TabsTrigger>
                )}
                {canOperate && (
                  <TabsTrigger value="release" className={styles.deskTab}>
                    <LockKeyholeIcon aria-hidden="true" /> {t("governance.desk.release")}
                  </TabsTrigger>
                )}
              </TabsList>
              <SlidersHorizontalIcon className={styles.instrumentMark} aria-hidden="true" />
            </div>
            {canReview && activeDesk === "review" && <TabsContent value="review" className={styles.deskBody}><ReviewDesk actor={actor} /></TabsContent>}
            {canOperate && activeDesk === "release" && <TabsContent value="release" className={styles.deskBody}><ReleaseDesk key={releaseDraftRevision} actor={actor} onDirtyChange={setReleaseDirty} /></TabsContent>}
          </Tabs>
        )}
      </div>
      <MutationConfirmation
        open={Boolean(pendingDesk)}
        onOpenChange={(open) => !open && setPendingDesk(null)}
        title={t("governance.unsaved.title")}
        description={t("governance.unsaved.description")}
        confirmLabel={t("governance.unsaved.discard")}
        cancelLabel={t("governance.unsaved.keepEditing")}
        destructive
        pending={false}
        onConfirm={() => {
          const nextDesk = pendingDesk
          setPendingDesk(null)
          if (nextDesk) {
            setReleaseDraftRevision((value) => value + 1)
            updateGovernanceUrl({ desk: nextDesk }, "push")
          }
        }}
      />
    </div>
  )
}
