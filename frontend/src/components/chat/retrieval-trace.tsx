"use client"

import { useMemo } from "react"
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react"
import { FileTextIcon, SearchIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import type { SourceSummary } from "@/lib/api/types"

interface TraceNodeData extends Record<string, unknown> {
  label: string
  meta: string
  kind: "query" | "source"
  origin?: string
}

type TraceNode = Node<TraceNodeData, "trace">

function TraceNodeComponent({ data }: NodeProps<TraceNode>) {
  const Icon = data.kind === "query" ? SearchIcon : FileTextIcon
  return (
    <div
      className="trace-pin-card"
      data-kind={data.kind}
      data-origin={data.origin}
    >
      <span className="trace-map-pin" aria-hidden="true" />
      {data.kind === "source" && <Handle type="target" position={Position.Top} />}
      <div className="flex items-start gap-2">
        <span className="trace-node-icon">
          <Icon className="size-3.5" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <p className="trace-node-label">{data.label}</p>
          <p className="trace-node-meta">{data.meta}</p>
        </div>
      </div>
      {data.kind === "query" && <Handle type="source" position={Position.Bottom} />}
    </div>
  )
}

const nodeTypes = { trace: TraceNodeComponent }

export function RetrievalTrace({ question, sources }: { question: string; sources: SourceSummary[] }) {
  const { t } = useLocale()
  const { nodes, edges } = useMemo(() => {
    const queryNode: TraceNode = {
      id: "query",
      type: "trace",
      position: { x: 140, y: 10 },
      data: { label: question, meta: t("chat.evidence.currentQuery"), kind: "query" },
    }
    const sourceNodes: TraceNode[] = sources.slice(0, 4).map((source, index) => ({
      id: `source-${index}`,
      type: "trace",
      position: { x: index % 2 === 0 ? 10 : 270, y: 155 + Math.floor(index / 2) * 125 },
      data: {
        label: source.title || t("chat.evidence.untitled"),
        meta: t("chat.evidence.retrievalScore", { score: Math.round(source.score * 100) }),
        kind: "source",
        origin: source.knowledge_origin,
      },
    }))
    const traceEdges: Edge[] = sourceNodes.map((source) => ({
      id: `query-${source.id}`,
      source: "query",
      target: source.id,
      type: "smoothstep",
      label: t("chat.evidence.retrieved"),
      style: {
        stroke:
          source.data.origin === "scientific_evidence"
            ? "#65a9c4"
            : source.data.origin === "personal_interpretation"
              ? "#7561a8"
              : "#c79b4b",
        strokeOpacity: 0.62,
      },
      labelStyle: { fill: "var(--muted-foreground)", fontSize: 10 },
    }))
    return { nodes: [queryNode, ...sourceNodes], edges: traceEdges }
  }, [question, sources, t])

  return (
    <div className="evidence-thread-map h-[25rem] overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.55}
        maxZoom={1.4}
        nodesConnectable={false}
        nodesDraggable={false}
        proOptions={{ hideAttribution: true }}
        aria-label={t("chat.evidence.traceLabel")}
      >
        <Background color="#c79b4b24" gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
