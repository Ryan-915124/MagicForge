"use client"

import { memo, useState, type FormEvent } from "react"
import { ArchiveRestoreIcon, SearchIcon, SlidersHorizontalIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { KnowledgeOrigin, MagicDomain } from "@/lib/api/types"

export interface ArchiveFilters {
  query: string
  origin: "all" | KnowledgeOrigin
  domain: "all" | MagicDomain
  level: "all" | "empirical" | "review" | "practitioner" | "anecdotal"
}

export const DEFAULT_ARCHIVE_FILTERS: ArchiveFilters = {
  query: "",
  origin: "all",
  domain: "all",
  level: "all",
}

const originItemDefinitions = [
  { value: "all", labelKey: "evidence.catalogue.allOrigins" },
  { value: "scientific_evidence", labelKey: "evidence.catalogue.scientificEvidence" },
  { value: "expert_practice", labelKey: "evidence.catalogue.expertPractice" },
  { value: "personal_interpretation", labelKey: "evidence.catalogue.interpretation" },
] as const

const domainItemDefinitions = [
  { value: "all", labelKey: "evidence.catalogue.allDomains" },
  { value: "card", labelKey: "evidence.catalogue.domainCard" },
  { value: "close-up", labelKey: "evidence.catalogue.domainCloseUp" },
  { value: "stage", labelKey: "evidence.catalogue.domainStage" },
  { value: "mentalism", labelKey: "evidence.catalogue.domainMentalism" },
  { value: "theory", labelKey: "evidence.catalogue.domainTheory" },
] as const

const levelItemDefinitions = [
  { value: "all", labelKey: "evidence.catalogue.allLevels" },
  { value: "empirical", labelKey: "evidence.catalogue.levelEmpirical" },
  { value: "review", labelKey: "evidence.catalogue.levelReview" },
  { value: "practitioner", labelKey: "evidence.catalogue.levelPractitioner" },
  { value: "anecdotal", labelKey: "evidence.catalogue.levelAnecdotal" },
] as const

interface ArchiveCatalogueProps {
  appliedFilters: ArchiveFilters
  pending: boolean
  disabled?: boolean
  onRetrieve: (filters: ArchiveFilters) => void
}

function ArchiveCatalogueComponent({ appliedFilters, pending, disabled = false, onRetrieve }: ArchiveCatalogueProps) {
  const { t } = useLocale()
  const [query, setQuery] = useState(appliedFilters.query)
  const [origin, setOrigin] = useState<ArchiveFilters["origin"]>(appliedFilters.origin)
  const [domain, setDomain] = useState<ArchiveFilters["domain"]>(appliedFilters.domain)
  const [level, setLevel] = useState<ArchiveFilters["level"]>(appliedFilters.level)

  const activeFilters = [origin !== "all", domain !== "all", level !== "all"].filter(Boolean).length
  const originItems = originItemDefinitions.map((item) => ({
    value: item.value,
    label: t(item.labelKey),
  }))
  const domainItems = domainItemDefinitions.map((item) => ({
    value: item.value,
    label: t(item.labelKey),
  }))
  const levelItems = levelItemDefinitions.map((item) => ({
    value: item.value,
    label: t(item.labelKey),
  }))

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onRetrieve({ query: query.trim(), origin, domain, level })
  }

  function browseAll() {
    setQuery("")
    setOrigin("all")
    setDomain("all")
    setLevel("all")
    onRetrieve(DEFAULT_ARCHIVE_FILTERS)
  }

  return (
    <section className="evidence-catalogue-drawer" aria-labelledby="catalogue-title">
      <div className="evidence-catalogue-drawer-handle" aria-hidden="true" />
      <div className="evidence-catalogue-title-row">
        <div>
          <span className="evidence-catalogue-number">{t("evidence.catalogue.cabinet")}</span>
          <h2 id="catalogue-title">{t("evidence.catalogue.title")}</h2>
        </div>
        <span className="evidence-filter-tally">
          <SlidersHorizontalIcon aria-hidden="true" />
          {t("evidence.catalogue.active", { count: activeFilters })}
        </span>
      </div>

      <form role="search" onSubmit={submit} aria-label={t("evidence.catalogue.searchLabel")} data-disabled={disabled}>
        <FieldGroup className="evidence-catalogue-fields">
          <Field data-disabled={disabled}>
            <FieldLabel htmlFor="archive-query">{t("evidence.catalogue.queryLabel")}</FieldLabel>
            <InputGroup className="evidence-index-slip">
              <InputGroupAddon>
                <SearchIcon aria-hidden="true" />
              </InputGroupAddon>
              <InputGroupInput
                id="archive-query"
                name="q"
                autoComplete="off"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("evidence.catalogue.queryPlaceholder")}
                aria-describedby="archive-query-description"
                disabled={disabled}
              />
            </InputGroup>
            <FieldDescription id="archive-query-description">
              {t("evidence.catalogue.queryDescription")}
            </FieldDescription>
          </Field>

          <div className="evidence-catalogue-facets">
            <Field data-disabled={disabled}>
              <FieldLabel htmlFor="archive-origin">{t("evidence.catalogue.originDrawer")}</FieldLabel>
              <Select
                items={originItems}
                name="origin"
                autoComplete="off"
                value={origin}
                disabled={disabled}
                onValueChange={(value) => setOrigin((value as ArchiveFilters["origin"] | null) ?? "all")}
              >
                <SelectTrigger id="archive-origin" className="evidence-catalogue-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {originItems.map((item) => (
                      <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>

            <Field data-disabled={disabled}>
              <FieldLabel htmlFor="archive-domain">{t("evidence.catalogue.domainDrawer")}</FieldLabel>
              <Select
                items={domainItems}
                name="domain"
                autoComplete="off"
                value={domain}
                disabled={disabled}
                onValueChange={(value) => setDomain((value as ArchiveFilters["domain"] | null) ?? "all")}
              >
                <SelectTrigger id="archive-domain" className="evidence-catalogue-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {domainItems.map((item) => (
                      <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>

            <Field data-disabled={disabled}>
              <FieldLabel htmlFor="archive-level">{t("evidence.catalogue.levelDrawer")}</FieldLabel>
              <Select
                items={levelItems}
                name="level"
                autoComplete="off"
                value={level}
                disabled={disabled}
                onValueChange={(value) => setLevel((value as ArchiveFilters["level"] | null) ?? "all")}
              >
                <SelectTrigger id="archive-level" className="evidence-catalogue-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {levelItems.map((item) => (
                      <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          </div>

          <div className="evidence-catalogue-actions">
            <Button type="submit" className="evidence-retrieve-button" disabled={pending || disabled}>
              <SearchIcon data-icon="inline-start" aria-hidden="true" />
              {pending ? t("evidence.catalogue.retrieving") : t("evidence.catalogue.retrieve")}
            </Button>
            <Button type="button" variant="ghost" className="evidence-browse-button" onClick={browseAll} disabled={pending || disabled}>
              <ArchiveRestoreIcon data-icon="inline-start" aria-hidden="true" />
              {t("evidence.catalogue.browseAll")}
            </Button>
          </div>
        </FieldGroup>
      </form>
    </section>
  )
}

export const ArchiveCatalogue = memo(ArchiveCatalogueComponent)
