"use client"

import {
  memo,
  useEffect,
  useMemo,
  type CSSProperties,
} from "react"
import {
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
  useStore,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react"
import { useReducedMotion } from "framer-motion"
import {
  ApertureIcon,
  BookOpenTextIcon,
  BrainCircuitIcon,
  CircleHelpIcon,
  FocusIcon,
  HandIcon,
  LibraryBigIcon,
  MinusIcon,
  NetworkIcon,
  PlusIcon,
  ScrollTextIcon,
  SparklesIcon,
  TelescopeIcon,
  TheaterIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Button } from "@/components/ui/button"
import type {
  EntityType,
  KnowledgeNodeVersion,
  KnowledgeRelationship,
  RelationType,
} from "@/lib/api/types"
import { humanize } from "@/lib/format"

import styles from "./knowledge-explorer.module.css"

interface KnowledgeNodeData extends Record<string, unknown> {
  node: KnowledgeNodeVersion
  isFocus: boolean
  focusNodeId: string
  revealOrder: number
  onActivate: (node: KnowledgeNodeVersion) => void
}

interface KnowledgeEdgeData extends Record<string, unknown> {
  relationship: KnowledgeRelationship
  sourceName: string
  targetName: string
  parallelIndex: number
  parallelTotal: number
}

type AtlasNode = Node<KnowledgeNodeData, "knowledge-artifact">
type AtlasEdge = Edge<KnowledgeEdgeData, "knowledge-relation">

const ATLAS_MIN_ZOOM = 0.56
const ATLAS_MAX_ZOOM = 1.45
const DESKTOP_CONSOLE_VIEWPORT_OFFSET = 64
const MOBILE_CONSOLE_VIEWPORT_OFFSET = 96

const nodeAppearance: Record<EntityType, { tone: string; icon: LucideIcon; label: string }> = {
  effect: { tone: "#d6b45f", icon: TheaterIcon, label: "Effect · impossible event" },
  technique: { tone: "#c9a75b", icon: HandIcon, label: "Technique · crafted action" },
  method: { tone: "#92556e", icon: WrenchIcon, label: "Method · concealed apparatus" },
  psychology_principle: { tone: "#a478ad", icon: ApertureIcon, label: "Psychology · influence field" },
  cognitive_mechanism: { tone: "#70b8cc", icon: BrainCircuitIcon, label: "Mechanism · cognitive model" },
  performer: { tone: "#ad6f7f", icon: TheaterIcon, label: "Performer · performance legacy" },
  source: { tone: "#d8c7a3", icon: ScrollTextIcon, label: "Source · manuscript fragment" },
  research_paper: { tone: "#82bdc9", icon: BookOpenTextIcon, label: "Paper · research artifact" },
}

const relationClass: Record<RelationType, string> = {
  uses: styles.edgeUses,
  inspired_by: styles.edgeInspired,
  requires: styles.edgeRequires,
  explains: styles.edgeExplains,
  performed_by: styles.edgePerformed,
  related_to: styles.edgeRelated,
}

const relationTone: Record<RelationType, string> = {
  uses: "#c3a05a",
  inspired_by: "#d8c7a3",
  requires: "#b99b63",
  explains: "#67afc2",
  performed_by: "#9b5c78",
  related_to: "#738096",
}

const orbitSectors: Record<EntityType, number> = {
  effect: -90,
  technique: 172,
  method: 138,
  psychology_principle: -28,
  cognitive_mechanism: 18,
  performer: -142,
  source: 96,
  research_paper: 58,
}

const relationOrbit: Record<RelationType, number> = {
  uses: 420,
  requires: 400,
  performed_by: 430,
  explains: 470,
  inspired_by: 520,
  related_to: 540,
}

function originMark(node: KnowledgeNodeVersion) {
  if (node.knowledge_origin === "scientific_evidence") return "SCI"
  if (node.knowledge_origin === "expert_practice") return "PRACTICE"
  return "INTERPRETATION"
}

const PerformerLegacy = memo(function PerformerLegacy({
  name,
  evidenceCount,
  origin,
}: {
  name: string
  evidenceCount: number
  origin: string
}) {
  const { t } = useLocale()

  return (
    <span className={styles.performerLegacy} aria-hidden="true">
      <span className={styles.performerSpotlight} />
      <span className={styles.performerFlyRail} />
      <span className={`${styles.performerCurtain} ${styles.performerCurtainLeft}`} />
      <span className={`${styles.performerCurtain} ${styles.performerCurtainRight}`} />
      <span className={styles.performerLegend}>{t("knowledge.graph.performerLegacy")}</span>
      <strong className={styles.performerName}>{name}</strong>
      <span className={styles.performerSignatureRule} />
      <span className={styles.performerFootlights} />
      <span className={styles.performerLedger}>
        <span>{origin}</span>
        <span>{t("knowledge.graph.traceCount", { count: String(evidenceCount).padStart(2, "0") })}</span>
      </span>
    </span>
  )
})

export const KnowledgeArtifactNode = memo(function KnowledgeArtifactNode({
  id,
  data,
  selected,
}: NodeProps<AtlasNode>) {
  const appearance = nodeAppearance[data.node.entity.type]
  const Icon = appearance.icon
  const evidenceCount = data.node.supporting_evidence_ids.length
  const { t } = useLocale()
  const { getNode, getZoom, setCenter } = useReactFlow<AtlasNode, AtlasEdge>()
  const reducedMotion = useReducedMotion()

  const keepFocusedObjectInView = () => {
    if (!window.matchMedia("(max-width: 760px)").matches) return

    const focusedNode = getNode(id)
    const centralNode = getNode(data.focusNodeId)
    if (!focusedNode) return

    const centerOf = (node: AtlasNode) => ({
      x: node.position.x + (node.measured?.width ?? node.width ?? 176) / 2,
      y: node.position.y + (node.measured?.height ?? node.height ?? 192) / 2,
    })
    const focusedCenter = centerOf(focusedNode)
    const centralCenter = centralNode ? centerOf(centralNode) : focusedCenter

    void setCenter(
      (focusedCenter.x + centralCenter.x) / 2,
      (focusedCenter.y + centralCenter.y) / 2,
      {
        zoom: Math.max(getZoom(), 0.58),
        duration: reducedMotion ? 0 : 320,
      }
    )
  }

  return (
    <article
      className={styles.nodeArtifact}
      data-entity={data.node.entity.type}
      data-focus={data.isFocus ? "true" : "false"}
      data-selected={selected ? "true" : "false"}
      style={
        {
          "--node-tone": appearance.tone,
          "--reveal-order": data.revealOrder,
        } as CSSProperties
      }
    >
      <Handle
        className={styles.nodeHandle}
        type="target"
        position={Position.Left}
        isConnectable={false}
      />
      <button
        type="button"
        className={styles.nodeTrigger}
        data-atlas-node={id}
        onClick={() => data.onActivate(data.node)}
        onFocus={keepFocusedObjectInView}
        aria-label={t("knowledge.graph.openNode", {
          type: humanize(data.node.entity.type),
          name: data.node.entity.name,
          count: evidenceCount,
        })}
      >
        {data.node.entity.type === "performer" ? (
          <PerformerLegacy
            name={data.node.entity.name}
            evidenceCount={evidenceCount}
            origin={originMark(data.node)}
          />
        ) : (
          <>
            <span className={styles.entityPresence} aria-hidden="true">
              <span className={styles.nodeOptic}>
                <Icon />
              </span>
            </span>
            <span className={styles.nodeCopy}>
              <span className={styles.nodeKind}>{appearance.label}</span>
              <strong>{data.node.entity.name}</strong>
            </span>
            <span className={styles.nodeCalibration} aria-hidden="true">
              <span>{originMark(data.node)}</span>
              <span>{t("knowledge.graph.traceCount", { count: String(evidenceCount).padStart(2, "0") })}</span>
            </span>
          </>
        )}
      </button>
      <Handle
        className={styles.nodeHandle}
        type="source"
        position={Position.Right}
        isConnectable={false}
      />
    </article>
  )
})

export const KnowledgeRelationEdge = memo(function KnowledgeRelationEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
}: EdgeProps<AtlasEdge>) {
  const relationship = data?.relationship
  const relation = relationship?.type ?? "related_to"
  const deltaX = targetX - sourceX
  const deltaY = targetY - sourceY
  const distance = Math.max(Math.hypot(deltaX, deltaY), 1)
  const parallelIndex = data?.parallelIndex ?? 0
  const parallelTotal = data?.parallelTotal ?? 1
  const parallelOffset = (parallelIndex - (parallelTotal - 1) / 2) * 28
  const bend = Math.min(72, distance * 0.13) + parallelOffset
  const bendDirection = sourceY <= targetY ? -1 : 1
  const controlX = (sourceX + targetX) / 2 + (-deltaY / distance) * bend * bendDirection
  const controlY = (sourceY + targetY) / 2 + (deltaX / distance) * bend * bendDirection
  const edgePath = `M ${sourceX},${sourceY} Q ${controlX},${controlY} ${targetX},${targetY}`
  const labelX = (sourceX + 2 * controlX + targetX) / 4
  const labelY = (sourceY + 2 * controlY + targetY) / 4

  return (
    <>
      <path
        d={edgePath}
        className={`${styles.edgeGlow} ${relationClass[relation]}`}
        style={{ "--edge-tone": relationTone[relation] } as CSSProperties}
        aria-hidden="true"
      />
      <BaseEdge
        id={id}
        path={edgePath}
        className={`${styles.edgePath} ${relationClass[relation]}`}
        style={{ "--edge-tone": relationTone[relation] } as CSSProperties}
        interactionWidth={20}
      />
      <path
        d={edgePath}
        pathLength={1}
        className={styles.edgeTracer}
        style={{ "--edge-tone": relationTone[relation] } as CSSProperties}
        aria-hidden="true"
      />
      <circle
        cx={targetX}
        cy={targetY}
        r={3.2}
        className={styles.edgeArrival}
        style={{ "--edge-tone": relationTone[relation] } as CSSProperties}
        aria-hidden="true"
      />
      <EdgeLabelRenderer>
        <div
          className={styles.edgeLabel}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          aria-hidden="true"
        >
          <span>{humanize(relation)}</span>
        </div>
      </EdgeLabelRenderer>
    </>
  )
})

const nodeTypes = { "knowledge-artifact": KnowledgeArtifactNode }
const edgeTypes = { "knowledge-relation": KnowledgeRelationEdge }

function buildVisibleAtlas(
  nodes: KnowledgeNodeVersion[],
  relationships: KnowledgeRelationship[],
  focusId: string | null,
  depth: 0 | 1,
  selectedId: string | null,
  onActivate: (node: KnowledgeNodeVersion) => void
) {
  const nodesById = new Map(nodes.map((node) => [node.entity.id, node]))
  const focus = focusId ? nodesById.get(focusId) ?? null : null
  if (!focus) {
    return {
      nodes: [] as AtlasNode[],
      edges: [] as AtlasEdge[],
      neighborCount: 0,
      firstNeighborId: null,
    }
  }

  const touching = relationships.filter(
    (relationship) =>
      (relationship.source_id === focus.entity.id || relationship.target_id === focus.entity.id) &&
      nodesById.has(relationship.source_id) &&
      nodesById.has(relationship.target_id)
  )
  const neighborIds = Array.from(new Set(touching.map((relationship) =>
    relationship.source_id === focus.entity.id ? relationship.target_id : relationship.source_id
  )))
  const firstNeighborId = neighborIds
    .map((id) => nodesById.get(id))
    .filter((node): node is KnowledgeNodeVersion => Boolean(node))
    .sort(
      (left, right) =>
        orbitSectors[left.entity.type] - orbitSectors[right.entity.type] ||
        left.entity.name.localeCompare(right.entity.name)
    )[0]?.entity.id ?? null
  const visibleIds = new Set([focus.entity.id, ...(depth === 1 ? neighborIds : [])])
  const visibleNodes = nodes.filter((node) => visibleIds.has(node.entity.id))
  const orderedVisibleNodes = [
    focus,
    ...visibleNodes
      .filter((node) => node.entity.id !== focus.entity.id)
      .sort((left, right) =>
        orbitSectors[left.entity.type] - orbitSectors[right.entity.type] ||
        left.entity.name.localeCompare(right.entity.name)
      ),
  ]
  const typeCounts = new Map<EntityType, number>()
  const typeTotals = new Map<EntityType, number>()
  for (const node of orderedVisibleNodes) {
    if (node.entity.id === focus.entity.id) continue
    typeTotals.set(node.entity.type, (typeTotals.get(node.entity.type) ?? 0) + 1)
  }

  const flowNodes = orderedVisibleNodes.map<AtlasNode>((node, visibleIndex) => {
    const isFocus = node.entity.id === focus.entity.id
    const typeIndex = typeCounts.get(node.entity.type) ?? 0
    typeCounts.set(node.entity.type, typeIndex + 1)
    const relationship = touching.find(
      (candidate) => candidate.source_id === node.entity.id || candidate.target_id === node.entity.id
    )
    const typeTotal = typeTotals.get(node.entity.type) ?? 1
    const angleOffset = (typeIndex - (typeTotal - 1) / 2) * 17
    const angle = (orbitSectors[node.entity.type] + angleOffset) * (Math.PI / 180)
    const radius = relationship ? relationOrbit[relationship.type] + (typeIndex % 2) * 34 : 440
    const position = isFocus
      ? { x: -184, y: -144 }
      : {
          x: Math.cos(angle) * radius - 88,
          y: Math.sin(angle) * radius * 0.68 - 88,
        }

    return {
      id: node.entity.id,
      type: "knowledge-artifact",
      position,
      draggable: false,
      focusable: false,
      selected: selectedId === node.entity.id,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      data: {
        node,
        isFocus,
        focusNodeId: focus.entity.id,
        revealOrder: isFocus ? 0 : visibleIndex,
        onActivate,
      },
    }
  })

  const visibleRelationships = touching.filter(
    (relationship) =>
      depth === 1 &&
      visibleIds.has(relationship.source_id) &&
      visibleIds.has(relationship.target_id)
  )
  const pairTotals = new Map<string, number>()
  for (const relationship of visibleRelationships) {
    const pairKey = [relationship.source_id, relationship.target_id].sort().join(":")
    pairTotals.set(pairKey, (pairTotals.get(pairKey) ?? 0) + 1)
  }
  const pairSeen = new Map<string, number>()

  const flowEdges = visibleRelationships.map<AtlasEdge>((relationship) => {
    const pairKey = [relationship.source_id, relationship.target_id].sort().join(":")
    const parallelIndex = pairSeen.get(pairKey) ?? 0
    pairSeen.set(pairKey, parallelIndex + 1)
    return {
      id: relationship.id,
      type: "knowledge-relation",
      source: relationship.source_id,
      target: relationship.target_id,
      focusable: false,
      selectable: false,
      ariaLabel: `${nodesById.get(relationship.source_id)?.entity.name ?? "Knowledge"} ${humanize(relationship.type)} ${nodesById.get(relationship.target_id)?.entity.name ?? "knowledge"}`,
      data: {
        relationship,
        sourceName: nodesById.get(relationship.source_id)?.entity.name ?? relationship.source_id,
        targetName: nodesById.get(relationship.target_id)?.entity.name ?? relationship.target_id,
        parallelIndex,
        parallelTotal: pairTotals.get(pairKey) ?? 1,
      },
    }
  })

  return { nodes: flowNodes, edges: flowEdges, neighborCount: neighborIds.length, firstNeighborId }
}

function ViewportSynchronizer({
  focusId,
  selectedId,
  depth,
  nodeCount,
}: {
  focusId: string | null
  selectedId: string | null
  depth: 0 | 1
  nodeCount: number
}) {
  const { fitView, getViewport, setCenter, setViewport } = useReactFlow<AtlasNode, AtlasEdge>()
  const reducedMotion = useReducedMotion()
  const artifactOpen = Boolean(selectedId)

  useEffect(() => {
    let settleFrame = 0
    const frame = window.requestAnimationFrame(() => {
      settleFrame = window.requestAnimationFrame(() => {
        const compact = window.matchMedia("(max-width: 760px)").matches
        if (nodeCount === 1) {
          void setCenter(0, compact ? MOBILE_CONSOLE_VIEWPORT_OFFSET : 0, {
            zoom: 0.9,
            duration: reducedMotion ? 0 : 420,
          })
          return
        }
        void fitView({
          padding: compact ? 0.14 : 0.22,
          minZoom: compact ? 0.58 : 0.46,
          maxZoom: 0.92,
          duration: reducedMotion ? 0 : 560,
        }).then(() => {
          const viewport = getViewport()
          void setViewport(
            {
              ...viewport,
              y:
                viewport.y -
                (compact
                  ? MOBILE_CONSOLE_VIEWPORT_OFFSET
                  : DESKTOP_CONSOLE_VIEWPORT_OFFSET),
            },
            { duration: reducedMotion ? 0 : 220 }
          )
        })
      })
    })
    return () => {
      window.cancelAnimationFrame(frame)
      window.cancelAnimationFrame(settleFrame)
    }
  }, [
    artifactOpen,
    depth,
    fitView,
    focusId,
    getViewport,
    nodeCount,
    reducedMotion,
    setCenter,
    setViewport,
  ])

  return null
}

function AtlasControls({
  canReveal,
  depth,
  firstNeighborId,
  neighborCount,
  onReveal,
}: {
  canReveal: boolean
  depth: 0 | 1
  firstNeighborId: string | null
  neighborCount: number
  onReveal: () => void
}) {
  const { t } = useLocale()
  const { fitView, zoomIn, zoomOut } = useReactFlow<AtlasNode, AtlasEdge>()
  const zoom = useStore((state) => state.transform[2])
  const reducedMotion = useReducedMotion()
  const duration = reducedMotion ? 0 : 280
  const zoomPercent = Math.round(zoom * 100)
  const canZoomOut = zoom > ATLAS_MIN_ZOOM + 0.005
  const canZoomIn = zoom < ATLAS_MAX_ZOOM - 0.005
  const revealOrbit = () => {
    const shouldFocusDiscovery = depth === 0 && firstNeighborId
    onReveal()

    if (!shouldFocusDiscovery) return
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const discoveredNode = Array.from(
          document.querySelectorAll<HTMLButtonElement>("[data-atlas-node]")
        ).find((button) => button.dataset.atlasNode === firstNeighborId)
        discoveredNode?.focus({ preventScroll: true })
      })
    })
  }

  return (
    <Panel position="bottom-left" className={styles.atlasControls}>
      <section className={styles.observatoryConsole} aria-labelledby="observatory-console-title">
        <div className={styles.consoleRegister}>
          <span id="observatory-console-title" className={styles.consoleIdentity}>
            <TelescopeIcon aria-hidden="true" />
            {t("knowledge.graph.observatoryDrive")}
          </span>
          <span className={styles.consoleScale} aria-hidden="true" />
          <output className={styles.consoleMagnification} aria-label={t("knowledge.graph.magnificationAria", { percent: zoomPercent })}>
            <small>{t("knowledge.graph.magnification")}</small>
            <strong>{zoomPercent}%</strong>
          </output>
        </div>

        <div className={styles.consoleDeck}>
          <div className={styles.opticalControls} role="group" aria-label={t("knowledge.graph.controlsAria")}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={styles.instrumentButton}
              onClick={() => void zoomOut({ duration })}
              disabled={!canZoomOut}
              aria-label={t("knowledge.graph.pullBackAria")}
            >
              <MinusIcon aria-hidden="true" />
              <span>{t("knowledge.graph.retreat")}</span>
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={styles.instrumentButton}
              onClick={() => void fitView({ padding: 0.32, duration })}
              aria-label={t("knowledge.graph.centerAria")}
            >
              <FocusIcon aria-hidden="true" />
              <span>{t("knowledge.graph.center")}</span>
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={styles.instrumentButton}
              onClick={() => void zoomIn({ duration })}
              disabled={!canZoomIn}
              aria-label={t("knowledge.graph.inspectAria")}
            >
              <PlusIcon aria-hidden="true" />
              <span>{t("knowledge.graph.inspect")}</span>
            </Button>
          </div>

          <Button
            type="button"
            variant="outline"
            size="lg"
            className={styles.revealButton}
            data-engaged={depth === 1 ? "true" : "false"}
            onClick={revealOrbit}
            disabled={!canReveal}
            aria-pressed={depth === 1}
            aria-controls="knowledge-constellation"
          >
            <NetworkIcon aria-hidden="true" />
            <span className={styles.revealCopy}>
              <strong>
                {depth === 0
                  ? canReveal
                    ? t("knowledge.graph.reveal")
                    : t("knowledge.graph.noPaths")
                  : t("knowledge.graph.conceal")}
              </strong>
              <small>{t(
                neighborCount === 1 ? "knowledge.graph.connectedOne" : "knowledge.graph.connectedMany",
                { count: neighborCount }
              )}</small>
            </span>
          </Button>
        </div>
      </section>
    </Panel>
  )
}

interface KnowledgeGraphProps {
  nodes: KnowledgeNodeVersion[]
  relationships: KnowledgeRelationship[]
  focusId: string | null
  selectedId: string | null
  depth: 0 | 1
  onActivate: (node: KnowledgeNodeVersion) => void
  onReveal: () => void
  onDismissArtifact: () => void
}

function KnowledgeGraphComponent({
  nodes,
  relationships,
  focusId,
  selectedId,
  depth,
  onActivate,
  onReveal,
  onDismissArtifact,
}: KnowledgeGraphProps) {
  const { t } = useLocale()
  const atlas = useMemo(
    () => buildVisibleAtlas(nodes, relationships, focusId, depth, selectedId, onActivate),
    [depth, focusId, nodes, onActivate, relationships, selectedId]
  )

  return (
    <div id="knowledge-constellation" className={styles.canvasField}>
      <div className={styles.stageAperture} aria-hidden="true" />
      <ReactFlow<AtlasNode, AtlasEdge>
        nodes={atlas.nodes}
        edges={atlas.edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        elementsSelectable
        panOnScroll
        zoomOnDoubleClick={false}
        minZoom={ATLAS_MIN_ZOOM}
        maxZoom={ATLAS_MAX_ZOOM}
        onPaneClick={onDismissArtifact}
        proOptions={{ hideAttribution: true }}
        aria-label={t("knowledge.graph.aria")}
      >
        <ViewportSynchronizer
          focusId={focusId}
          selectedId={selectedId}
          depth={depth}
          nodeCount={atlas.nodes.length}
        />
        <Panel position="top-left" className={styles.projectionSeal}>
          <LibraryBigIcon aria-hidden="true" />
          <span>{t("knowledge.graph.provenance")}</span>
        </Panel>
        {focusId && atlas.neighborCount === 0 ? (
          <Panel position="top-right" className={styles.isolatedNotice}>
            <CircleHelpIcon aria-hidden="true" />
            <span>{t("knowledge.graph.isolated")}</span>
          </Panel>
        ) : null}
        <AtlasControls
          canReveal={atlas.neighborCount > 0}
          depth={depth}
          firstNeighborId={atlas.firstNeighborId}
          neighborCount={atlas.neighborCount}
          onReveal={onReveal}
        />
      </ReactFlow>
      {atlas.nodes.length === 0 ? (
        <div className={styles.atlasEmpty}>
          <SparklesIcon aria-hidden="true" />
          <h2>{t("knowledge.graph.emptyTitle")}</h2>
          <p>{t("knowledge.graph.emptyDescription")}</p>
        </div>
      ) : null}
    </div>
  )
}

export const KnowledgeGraph = memo(KnowledgeGraphComponent)
