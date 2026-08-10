"use client"

import type { ReactNode } from "react"

import { useLocale } from "@/components/i18n/locale-provider"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type {
  ConfidenceAssessmentInput,
  ConfidenceDimensionInput,
  WorkflowStatus,
} from "@/lib/api/governance-types"

import styles from "./governance.module.css"

export interface SelectOption<T extends string> {
  value: T
  label: string
}

export function LabeledSelect<T extends string>({
  id,
  label,
  value,
  options,
  onChange,
  disabled,
  placeholder,
}: {
  id: string
  label: string
  value: T | null
  options: SelectOption<T>[]
  onChange: (value: T) => void
  disabled?: boolean
  placeholder?: string
}) {
  return (
    <Field>
      <FieldLabel htmlFor={id} className={styles.fieldLabel}>{label}</FieldLabel>
      <Select
        name={id}
        value={value}
        disabled={disabled}
        onValueChange={(next) => next && onChange(next as T)}
      >
        <SelectTrigger id={id} className={`${styles.select} w-full`}>
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent align="start">
          <SelectGroup>
            {options.map((option) => (
              <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </Field>
  )
}

export function StatusBadge({ status }: { status: WorkflowStatus | string }) {
  const { t } = useLocale()
  const labels: Record<string, string> = {
    submitted: t("governance.status.submitted"),
    approved: t("governance.status.approved"),
    rejected: t("governance.status.rejected"),
    superseded: t("governance.status.superseded"),
    revoked: t("governance.status.revoked"),
    pending: t("governance.status.pending"),
    authorized: t("governance.status.authorized"),
    ingested: t("governance.status.ingested"),
    staged: t("governance.status.staged"),
    active: t("governance.status.active"),
    inactive: t("governance.status.inactive"),
  }
  return <Badge variant="outline" className={styles.statusBadge} data-status={status}>{labels[status] ?? status}</Badge>
}

export function DetailValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className={styles.microLabel}>{label}</p>
      <div className={styles.detailValue}>{children || "—"}</div>
    </div>
  )
}

type DimensionKey = keyof Omit<ConfidenceAssessmentInput, "assessed_by">

export function blankConfidence(assessedBy: string): ConfidenceAssessmentInput {
  const empty: ConfidenceDimensionInput = { score: 0, reason: "" }
  return {
    provenance_quality: { ...empty },
    method_rigor: { ...empty },
    claim_directness: { ...empty },
    consistency: { ...empty },
    magic_applicability: { ...empty },
    assessed_by: assessedBy,
  }
}

export function ConfidenceEditor({
  value,
  onChange,
}: {
  value: ConfidenceAssessmentInput
  onChange: (value: ConfidenceAssessmentInput) => void
}) {
  const { t } = useLocale()
  const dimensions: Array<{ key: DimensionKey; label: string }> = [
    { key: "provenance_quality", label: t("governance.confidence.provenance") },
    { key: "method_rigor", label: t("governance.confidence.rigor") },
    { key: "claim_directness", label: t("governance.confidence.directness") },
    { key: "consistency", label: t("governance.confidence.consistency") },
    { key: "magic_applicability", label: t("governance.confidence.applicability") },
  ]
  const scoreOptions: SelectOption<"0" | "0.5" | "1">[] = [
    { value: "0", label: `0 · ${t("governance.confidence.unsupported")}` },
    { value: "0.5", label: `0.5 · ${t("governance.confidence.partial")}` },
    { value: "1", label: `1 · ${t("governance.confidence.strong")}` },
  ]

  const update = (key: DimensionKey, patch: Partial<ConfidenceDimensionInput>) => {
    onChange({ ...value, [key]: { ...value[key], ...patch } })
  }

  return (
    <div className={`${styles.confidenceGrid} ${styles.full}`}>
      {dimensions.map((dimension) => (
        <fieldset key={dimension.key} className={styles.confidenceCard}>
          <legend>{dimension.label}</legend>
          <div className={styles.confidenceControls}>
            <Select
              value={String(value[dimension.key].score)}
              onValueChange={(next) => {
                if (!next) return
                update(dimension.key, { score: Number(next) as 0 | 0.5 | 1 })
              }}
            >
              <SelectTrigger aria-label={t("governance.confidence.scoreLabel", { dimension: dimension.label })} className={styles.select}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                <SelectGroup>
                  {scoreOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <Input
              name={`confidence.${dimension.key}.reason`}
              autoComplete="off"
              className={styles.input}
              value={value[dimension.key].reason}
              onChange={(event) => update(dimension.key, { reason: event.target.value })}
              placeholder={t("governance.confidence.reason")}
              aria-label={t("governance.confidence.reasonLabel", { dimension: dimension.label })}
              required
            />
          </div>
        </fieldset>
      ))}
    </div>
  )
}

export function MutationConfirmation({
  open,
  title,
  description,
  details,
  confirmLabel,
  cancelLabel,
  destructive,
  pending,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  title: string
  description: string
  details?: Array<{ label: string; value: ReactNode }>
  confirmLabel: string
  cancelLabel?: string
  destructive?: boolean
  pending: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: () => void
}) {
  const { t } = useLocale()
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="border-[color:rgb(196_154_87_/_35%)] bg-[#101112]">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        {details && details.length > 0 ? (
          <dl className="grid max-h-64 grid-cols-[minmax(7rem,auto)_minmax(0,1fr)] gap-x-3 gap-y-2 overflow-y-auto overscroll-contain rounded-md border border-white/10 bg-black/20 p-3 text-xs">
            {details.map((detail, index) => (
              <div className="contents" key={`${detail.label}-${index}`}>
                <dt className="text-muted-foreground">{detail.label}</dt>
                <dd className="min-w-0 break-words font-mono text-foreground" translate="no">{detail.value || "—"}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>{cancelLabel ?? t("governance.action.cancel")}</AlertDialogCancel>
          <AlertDialogAction
            variant={destructive ? "destructive" : "default"}
            disabled={pending}
            onClick={onConfirm}
          >
            {pending ? t("governance.action.recording") : confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export function shortId(value: string) {
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}

export function lines(value: string) {
  return [...new Set(value.split("\n").map((line) => line.trim()).filter(Boolean))]
}

export function errorText(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}
