"use client"

import dynamic from "next/dynamic"
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react"
import {
  ApertureIcon,
  CompassIcon,
  FilterIcon,
  RotateCwIcon,
  SearchIcon,
  SparklesIcon,
} from "lucide-react"

import { MotionPage } from "@/components/app-shell/motion-page"
import { useLocale } from "@/components/i18n/locale-provider"
import { KnowledgeArtifactPanel } from "@/components/knowledge/knowledge-artifact-panel"
import { KnowledgeInstrumentHeader } from "@/components/knowledge/knowledge-instrument-header"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
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
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { MagicForgeApiError, magicForgeApi } from "@/lib/api/client"
import type {
  EntityType,
  EvidenceCard,
  GraphProjectionSummary,
  KnowledgeNodeVersion,
  KnowledgeOrigin,
  KnowledgeRelationship,
} from "@/lib/api/types"

import styles from "@/components/knowledge/knowledge-explorer.module.css"

const KnowledgeGraph = dynamic(
  () => import("@/components/knowledge/knowledge-graph").then((module) => module.KnowledgeGraph),
  { ssr: false, loading: () => <AtlasCalibrating /> }
)

type ExplorerStatus = "loading" | "ready" | "empty" | "error"

interface AtlasFilters {
  query: string
  entityType: "all" | EntityType
  origin: "all" | KnowledgeOrigin
}

const DEFAULT_FILTERS: AtlasFilters = { query: "", entityType: "all", origin: "all" }

const entityItems: Array<{ value: AtlasFilters["entityType"]; label: string }> = [
  { value: "all", label: "All concept families" },
  { value: "effect", label: "Effect" },
  { value: "technique", label: "Technique" },
  { value: "method", label: "Method" },
  { value: "psychology_principle", label: "Psychology Principle" },
  { value: "cognitive_mechanism", label: "Cognitive Mechanism" },
  { value: "performer", label: "Performer" },
  { value: "source", label: "Source" },
  { value: "research_paper", label: "Research Paper" },
]

const originItems: Array<{ value: AtlasFilters["origin"]; label: string }> = [
  { value: "all", label: "All epistemic layers" },
  { value: "scientific_evidence", label: "Scientific evidence" },
  { value: "expert_practice", label: "Expert practice" },
  { value: "personal_interpretation", label: "MagicForge interpretation" },
]

const entityValues = new Set<EntityType>(entityItems.flatMap((item) => item.value === "all" ? [] : [item.value]))
const originValues = new Set<KnowledgeOrigin>(originItems.flatMap((item) => item.value === "all" ? [] : [item.value]))

function AtlasCalibrating() {
  const { t } = useLocale()

  return (
    <div className={styles.calibratingField} role="status" aria-label={t("knowledge.state.calibratingAria")}>
      <div className={styles.calibrationRings} aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <Skeleton className={styles.calibrationArtifact} />
      <p>{t("knowledge.state.calibrating")}</p>
    </div>
  )
}

function explorerHref(filters: AtlasFilters, focusId?: string | null, depth: 0 | 1 = 0) {
  const params = new URLSearchParams()
  if (filters.query) params.set("q", filters.query)
  if (filters.entityType !== "all") params.set("entity", filters.entityType)
  if (filters.origin !== "all") params.set("origin", filters.origin)
  if (focusId) params.set("focus", focusId)
  if (depth === 1) params.set("depth", "1")
  if (params.size === 0) params.set("browse", "1")
  return `/knowledge?${params.toString()}`
}

function readExplorerLocation() {
  const params = new URLSearchParams(window.location.search)
  const rawEntity = params.get("entity")
  const rawOrigin = params.get("origin")
  return {
    filters: {
      query: params.get("q")?.trim() ?? "",
      entityType: rawEntity && entityValues.has(rawEntity as EntityType)
        ? rawEntity as AtlasFilters["entityType"]
        : "all",
      origin: rawOrigin && originValues.has(rawOrigin as KnowledgeOrigin)
        ? rawOrigin as AtlasFilters["origin"]
        : "all",
    } satisfies AtlasFilters,
    focusId: params.get("focus"),
    depth: params.get("depth") === "1" ? 1 as const : 0 as const,
  }
}

const compactInspectorQuery = "(max-width: 1319px)"
const compactSelectQuery = "(max-width: 760px)"

function subscribeCompactInspector(onChange: () => void) {
  const media = window.matchMedia(compactInspectorQuery)
  media.addEventListener("change", onChange)
  return () => media.removeEventListener("change", onChange)
}

function getCompactInspectorSnapshot() {
  return window.matchMedia(compactInspectorQuery).matches
}

function getCompactInspectorServerSnapshot() {
  return false
}

function subscribeCompactSelect(onChange: () => void) {
  const media = window.matchMedia(compactSelectQuery)
  media.addEventListener("change", onChange)
  return () => media.removeEventListener("change", onChange)
}

function getCompactSelectSnapshot() {
  return window.matchMedia(compactSelectQuery).matches
}

function getCompactSelectServerSnapshot() {
  return false
}

function focusConstellationObject(id: string) {
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLButtonElement>(`[data-atlas-node="${id}"]`)?.focus({ preventScroll: true })
    })
  })
}

function pickEntryNode(
  nodes: KnowledgeNodeVersion[],
  relationships: KnowledgeRelationship[],
  query: string,
  preferredFocusId?: string | null
) {
  if (preferredFocusId) {
    const preferred = nodes.find(
      (node) => node.entity.id === preferredFocusId || node.id === preferredFocusId
    )
    if (preferred) return preferred
  }

  const normalizedQuery = query.trim().toLocaleLowerCase()
  if (normalizedQuery) {
    return nodes.find((node) => node.entity.name.toLocaleLowerCase() === normalizedQuery) ?? nodes[0] ?? null
  }

  const connectedIds = new Set(
    relationships.flatMap((relationship) => [relationship.source_id, relationship.target_id])
  )
  const connected = nodes.filter((node) => connectedIds.has(node.entity.id))
  const curatedEntrances = ["misdirection", "sleight of hand", "psychological forcing", "attention"]
  for (const name of curatedEntrances) {
    const match = connected.find((node) => node.entity.name.toLocaleLowerCase() === name)
    if (match) return match
  }
  return connected.find((node) => node.entity.type === "technique") ?? connected[0] ?? nodes[0] ?? null
}

interface AtlasSearchConsoleProps {
  appliedFilters: AtlasFilters
  pending: boolean
  onSearch: (filters: AtlasFilters) => void
}

const AtlasSearchConsole = memo(function AtlasSearchConsole({
  appliedFilters,
  pending,
  onSearch,
}: AtlasSearchConsoleProps) {
  const { t } = useLocale()
  const [query, setQuery] = useState(appliedFilters.query)
  const [entityType, setEntityType] = useState<AtlasFilters["entityType"]>(appliedFilters.entityType)
  const [origin, setOrigin] = useState<AtlasFilters["origin"]>(appliedFilters.origin)
  const compactSelect = useSyncExternalStore(
    subscribeCompactSelect,
    getCompactSelectSnapshot,
    getCompactSelectServerSnapshot
  )
  const localizedEntityItems = useMemo(
    () => entityItems.map((item) => item.value === "all" ? { ...item, label: t("knowledge.search.allFamilies") } : item),
    [t]
  )
  const localizedOriginItems = useMemo(
    () => originItems.map((item) => item.value === "all" ? { ...item, label: t("knowledge.search.allLayers") } : item),
    [t]
  )

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onSearch({ query: query.trim(), entityType, origin })
  }

  function browseField() {
    setQuery("")
    setEntityType("all")
    setOrigin("all")
    onSearch(DEFAULT_FILTERS)
  }

  return (
    <form className={styles.focusAperture} role="search" aria-label={t("knowledge.search.aria")} onSubmit={submit}>
      <div className={styles.apertureIdentity}>
        <span aria-hidden="true"><ApertureIcon /></span>
        <div>
          <small>{t("knowledge.search.coordinates")}</small>
          <strong>{t("knowledge.search.pull")}</strong>
        </div>
      </div>
      <FieldGroup className={styles.apertureFields}>
        <Field className={styles.queryField}>
          <FieldLabel htmlFor="atlas-query">{t("knowledge.search.queryLabel")}</FieldLabel>
          <InputGroup>
            <InputGroupAddon><SearchIcon aria-hidden="true" /></InputGroupAddon>
            <InputGroupInput
              id="atlas-query"
              name="q"
              autoComplete="off"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("knowledge.search.placeholder")}
            />
          </InputGroup>
        </Field>
        <Field>
          <FieldLabel htmlFor="atlas-entity">{t("knowledge.search.family")}</FieldLabel>
          <Select
            items={localizedEntityItems}
            name="entity"
            autoComplete="off"
            value={entityType}
            onValueChange={(value) => setEntityType((value as AtlasFilters["entityType"] | null) ?? "all")}
          >
            <SelectTrigger id="atlas-entity" className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent
              align={compactSelect ? "end" : "start"}
              alignItemWithTrigger={false}
              side={compactSelect ? "top" : "bottom"}
              sideOffset={8}
            >
              <SelectGroup>
                {localizedEntityItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor="atlas-origin">{t("knowledge.search.layer")}</FieldLabel>
          <Select
            items={localizedOriginItems}
            name="origin"
            autoComplete="off"
            value={origin}
            onValueChange={(value) => setOrigin((value as AtlasFilters["origin"] | null) ?? "all")}
          >
            <SelectTrigger id="atlas-origin" className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent
              align="end"
              alignItemWithTrigger={false}
              side={compactSelect ? "top" : "bottom"}
              sideOffset={8}
            >
              <SelectGroup>
                {localizedOriginItems.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
      </FieldGroup>
      <div className={styles.apertureActions}>
        <Button type="submit" size="lg" disabled={pending}>
          {pending ? <Spinner data-icon="inline-start" /> : <CompassIcon data-icon="inline-start" aria-hidden="true" />}
          {pending ? t("knowledge.search.locating") : t("knowledge.search.locate")}
        </Button>
        <Button type="button" variant="ghost" size="lg" disabled={pending} onClick={browseField}>
          <FilterIcon data-icon="inline-start" aria-hidden="true" />
          {t("knowledge.search.clear")}
        </Button>
      </div>
    </form>
  )
})

export function KnowledgeExplorer() {
  const { t } = useLocale()
  const [filters, setFilters] = useState<AtlasFilters>(DEFAULT_FILTERS)
  const [nodes, setNodes] = useState<KnowledgeNodeVersion[]>([])
  const [relationships, setRelationships] = useState<KnowledgeRelationship[]>([])
  const [projection, setProjection] = useState<GraphProjectionSummary | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [selected, setSelected] = useState<KnowledgeNodeVersion | null>(null)
  const [depth, setDepth] = useState<0 | 1>(0)
  const [status, setStatus] = useState<ExplorerStatus>("loading")
  const [error, setError] = useState<MagicForgeApiError | null>(null)
  const [announcement, setAnnouncement] = useState("")
  const [evidenceCards, setEvidenceCards] = useState<EvidenceCard[]>([])
  const [evidenceLoading, setEvidenceLoading] = useState(false)
  const [evidenceFailed, setEvidenceFailed] = useState(false)
  const compactInspector = useSyncExternalStore(
    subscribeCompactInspector,
    getCompactInspectorSnapshot,
    getCompactInspectorServerSnapshot
  )
  const requestSequence = useRef(0)
  const evidenceSequence = useRef(0)
  const artifactOpener = useRef<string | null>(null)
  const translateRef = useRef(t)

  useEffect(() => {
    translateRef.current = t
  }, [t])

  const retrieve = useCallback(async (
    nextFilters: AtlasFilters,
    options: { updateHistory?: boolean; preferredFocusId?: string | null; preferredDepth?: 0 | 1 } = {}
  ) => {
    const translate = translateRef.current
    const requestId = ++requestSequence.current
    const normalized = { ...nextFilters, query: nextFilters.query.trim() }
    setFilters(normalized)
    setStatus("loading")
    setError(null)

    try {
      const response = await magicForgeApi.search({
        query: normalized.query,
        limit: 120,
        knowledge_types: ["effect", "technique", "method", "psychology", "performance"],
        entity_types: normalized.entityType === "all" ? [] : [normalized.entityType],
        knowledge_origins: normalized.origin === "all" ? [] : [normalized.origin],
      })
      if (requestId !== requestSequence.current) return

      let nextNodes = response.nodes
      const preferredId = options.preferredFocusId
      if (preferredId && !nextNodes.some((node) => node.entity.id === preferredId || node.id === preferredId)) {
        try {
          const directNode = await magicForgeApi.knowledgeNode(preferredId)
          if (requestId !== requestSequence.current) return
          nextNodes = [directNode, ...nextNodes.filter((node) => node.entity.id !== directNode.entity.id)]
        } catch {
          // A deep-linked node may no longer be available under the projection safety gate.
        }
      }

      const focus = pickEntryNode(nextNodes, response.relationships, normalized.query, preferredId)
      const canRevealFocus = Boolean(focus && response.relationships.some(
        (relationship) =>
          relationship.source_id === focus.entity.id || relationship.target_id === focus.entity.id
      ))
      setNodes(nextNodes)
      setRelationships(response.relationships)
      setProjection(response.projection)
      setFocusId(focus?.entity.id ?? null)
      setSelected(null)
      setEvidenceCards([])
      setEvidenceLoading(false)
      setEvidenceFailed(false)
      setDepth(options.preferredDepth === 1 && canRevealFocus ? 1 : 0)
      artifactOpener.current = null
      setStatus(nextNodes.length > 0 ? "ready" : "empty")
      setAnnouncement(
        nextNodes.length > 0
          ? translate("knowledge.state.loadedAnnouncement", {
              count: nextNodes.length,
              concept: focus?.entity.name ?? translate("knowledge.state.noConcept"),
            })
          : translate("knowledge.state.emptyAnnouncement")
      )

      if (options.updateHistory) {
        window.history.pushState(
          { magicforgeAtlas: true },
          "",
          explorerHref(normalized, focus?.entity.id)
        )
      }
    } catch (cause) {
      if (requestId !== requestSequence.current) return
      setStatus("error")
      setError(
        cause instanceof MagicForgeApiError
          ? cause
          : new MagicForgeApiError(translate("knowledge.state.failedAnnouncement"), "backend_error", 500)
      )
      setAnnouncement(translate("knowledge.state.failedAnnouncement"))
    }
  }, [])

  useEffect(() => {
    const location = readExplorerLocation()
    const frame = window.requestAnimationFrame(() => {
      void retrieve(location.filters, {
        preferredFocusId: location.focusId,
        preferredDepth: location.depth,
      })
    })

    function restoreLocation() {
      const restored = readExplorerLocation()
      void retrieve(restored.filters, {
        preferredFocusId: restored.focusId,
        preferredDepth: restored.depth,
      })
    }

    window.addEventListener("popstate", restoreLocation)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener("popstate", restoreLocation)
    }
  }, [retrieve])

  useEffect(() => {
    const sequence = ++evidenceSequence.current
    if (!selected) return

    const ids = selected.supporting_evidence_ids
    if (ids.length === 0) return
    void Promise.allSettled(ids.map((id) => magicForgeApi.evidence(id))).then((results) => {
      if (sequence !== evidenceSequence.current) return
      const cards = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : [])
      setEvidenceCards(cards)
      setEvidenceFailed(cards.length === 0)
      setEvidenceLoading(false)
      setAnnouncement(
        cards.length > 0
          ? translateRef.current("knowledge.state.evidenceLoaded", {
              count: cards.length,
              concept: selected.entity.name,
            })
          : translateRef.current("knowledge.state.evidenceFailed", { concept: selected.entity.name })
      )
    })
  }, [selected])

  const focusNode = useMemo(
    () => nodes.find((node) => node.entity.id === focusId) ?? null,
    [focusId, nodes]
  )

  const selectedRelationships = useMemo(() => {
    if (!selected) return []
    return relationships.filter(
      (relationship) =>
        relationship.source_id === selected.entity.id || relationship.target_id === selected.entity.id
    )
  }, [relationships, selected])

  const selectedRelatedNodes = useMemo(() => {
    if (!selected) return []
    const relatedIds = new Set(
      selectedRelationships.map((relationship) =>
        relationship.source_id === selected.entity.id ? relationship.target_id : relationship.source_id
      )
    )
    return nodes.filter((node) => relatedIds.has(node.entity.id))
  }, [nodes, selected, selectedRelationships])

  const candidateNodes = useMemo(() => {
    const ordered = focusNode ? [focusNode, ...nodes.filter((node) => node.entity.id !== focusNode.entity.id)] : nodes
    return ordered.slice(0, 8)
  }, [focusNode, nodes])

  const activateArtifact = useCallback((node: KnowledgeNodeVersion) => {
    artifactOpener.current = node.entity.id
    setSelected(node)
    setEvidenceCards([])
    setEvidenceLoading(node.supporting_evidence_ids.length > 0)
    setEvidenceFailed(false)
    setAnnouncement(translateRef.current("knowledge.state.artifactOpened", { concept: node.entity.name }))
  }, [])

  const focusArtifact = useCallback((node: KnowledgeNodeVersion) => {
    setFocusId(node.entity.id)
    setDepth(0)
    setSelected(null)
    setEvidenceCards([])
    setEvidenceLoading(false)
    setEvidenceFailed(false)
    artifactOpener.current = null
    setAnnouncement(translateRef.current("knowledge.state.artifactFocused", { concept: node.entity.name }))
    window.history.pushState(
      { magicforgeAtlas: true },
      "",
      explorerHref(filters, node.entity.id)
    )
  }, [filters])

  const closeArtifact = useCallback(() => {
    const opener = artifactOpener.current
    setSelected(null)
    setEvidenceCards([])
    setEvidenceLoading(false)
    setEvidenceFailed(false)
    artifactOpener.current = null
    setAnnouncement(translateRef.current("knowledge.state.artifactClosed"))
    if (opener) focusConstellationObject(opener)
  }, [])

  const revealPaths = useCallback(() => {
    const revealing = depth === 0
    if (!revealing && selected && selected.entity.id !== focusId) {
      setSelected(null)
      setEvidenceCards([])
      setEvidenceLoading(false)
      setEvidenceFailed(false)
      artifactOpener.current = null
    }
    const nextDepth = revealing ? 1 : 0
    setDepth(nextDepth)
    window.history.pushState(
      { magicforgeAtlas: true },
      "",
      explorerHref(filters, focusId, nextDepth)
    )
    setAnnouncement(
      revealing
        ? translateRef.current("knowledge.state.pathsRevealed", {
            concept: focusNode?.entity.name ?? translateRef.current("knowledge.state.centralFallback"),
          })
        : translateRef.current("knowledge.state.pathsHidden", {
            concept: focusNode?.entity.name ?? translateRef.current("knowledge.state.conceptFallback"),
          })
    )
  }, [depth, filters, focusId, focusNode, selected])

  const traceFromSelected = useCallback(() => {
    if (!selected) return
    const tracedId = selected.entity.id
    focusArtifact(selected)
    focusConstellationObject(tracedId)
  }, [focusArtifact, selected])

  return (
    <MotionPage className={styles.atlasPage}>
      <KnowledgeInstrumentHeader
        projection={projection}
        visibleNodes={nodes.length}
        visibleRelationships={relationships.length}
      />

      <div className={styles.understageRoom}>
        <div className={styles.overheadLight} aria-hidden="true" />
        <section className={styles.atlasInstrument} aria-labelledby="atlas-field-title">
          <h2 id="atlas-field-title" className="sr-only">{t("knowledge.state.fieldTitle")}</h2>
          <AtlasSearchConsole
            key={`${filters.query}:${filters.entityType}:${filters.origin}`}
            appliedFilters={filters}
            pending={status === "loading"}
            onSearch={(nextFilters) => void retrieve(nextFilters, { updateHistory: true })}
          />

          <div className={styles.instrumentLedger}>
            <div>
              <span>{t("knowledge.state.centralConcept")}</span>
              <strong>{focusNode?.entity.name ?? t("knowledge.state.noLocatedConcept")}</strong>
            </div>
            <div>
              <span>{t("knowledge.state.revealedDepth")}</span>
              <strong>{depth === 0 ? t("knowledge.state.centralDepth") : t("knowledge.state.oneOrbit")}</strong>
            </div>
            <div>
              <span>{t("knowledge.state.projectionGate")}</span>
              <strong>{projection
                ? t("knowledge.state.drawable", {
                    renderable: projection.renderable_relationships,
                    total: projection.relationships,
                  })
                : t("knowledge.state.pending")}</strong>
            </div>
            <div>
              <span>{t("knowledge.state.humanStatus")}</span>
              <strong>{projection?.human_verified ? t("knowledge.state.verified") : t("knowledge.state.unverifiedBootstrap")}</strong>
            </div>
          </div>

          {error ? (
            <Alert className={styles.atlasAlert}>
              <RotateCwIcon aria-hidden="true" />
              <AlertTitle>{t("knowledge.state.errorTitle")}</AlertTitle>
              <AlertDescription>
                {error.message} {t("knowledge.state.errorSuffix")}
                <Button type="button" variant="outline" size="sm" onClick={() => void retrieve(filters)}>
                  <RotateCwIcon data-icon="inline-start" aria-hidden="true" />
                  {t("knowledge.state.retry")}
                </Button>
              </AlertDescription>
            </Alert>
          ) : null}

          <div
            className={styles.canvasAssembly}
            data-artifact-open={selected ? "true" : "false"}
            aria-busy={status === "loading"}
          >
            {status === "loading" && nodes.length === 0 ? (
              <AtlasCalibrating />
            ) : (
              <KnowledgeGraph
                nodes={nodes}
                relationships={relationships}
                focusId={focusId}
                selectedId={selected?.entity.id ?? null}
                depth={depth}
                onActivate={activateArtifact}
                onReveal={revealPaths}
                onDismissArtifact={closeArtifact}
              />
            )}

            <KnowledgeArtifactPanel
              node={selected}
              evidenceCards={evidenceCards}
              evidenceLoading={evidenceLoading}
              evidenceFailed={evidenceFailed}
              relatedNodes={selectedRelatedNodes}
              relationships={selectedRelationships}
              compact={compactInspector}
              onOpenChange={(open) => {
                if (!open) closeArtifact()
              }}
              onTraceFromHere={traceFromSelected}
            />
          </div>

          <nav className={styles.entryRail} aria-label={t("knowledge.state.nearbyAria")}>
            <div className={styles.entryRailLabel}>
              <SparklesIcon aria-hidden="true" />
              <span>
                <small>{t("knowledge.state.nearbySignals")}</small>
                <strong>{t("knowledge.state.signalCount", { signals: candidateNodes.length, concepts: nodes.length })}</strong>
              </span>
            </div>
            <div className={styles.entryRailList}>
              {candidateNodes.map((node) => (
                <button
                  key={node.entity.id}
                  type="button"
                  data-active={node.entity.id === focusId ? "true" : "false"}
                  onClick={(event) => {
                    focusArtifact(node)
                    if (event.detail === 0) focusConstellationObject(node.entity.id)
                  }}
                >
                  <span className={styles.entrySignal} aria-hidden="true" />
                  <strong>{node.entity.name}</strong>
                  <small>{node.entity.type.replaceAll("_", " ")}</small>
                </button>
              ))}
            </div>
          </nav>
        </section>
      </div>

      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
    </MotionPage>
  )
}
