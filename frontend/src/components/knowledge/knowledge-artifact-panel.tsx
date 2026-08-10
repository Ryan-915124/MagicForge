"use client"

import { XIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { KnowledgeNodeCard } from "@/components/knowledge/knowledge-node-card"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import type {
  EvidenceCard,
  KnowledgeNodeVersion,
  KnowledgeRelationship,
} from "@/lib/api/types"

import styles from "./knowledge-explorer.module.css"

interface KnowledgeArtifactPanelProps {
  node: KnowledgeNodeVersion | null
  evidenceCards: EvidenceCard[]
  evidenceLoading: boolean
  evidenceFailed: boolean
  relatedNodes: KnowledgeNodeVersion[]
  relationships: KnowledgeRelationship[]
  compact: boolean
  onOpenChange: (open: boolean) => void
  onTraceFromHere: () => void
}

function ArtifactContents(props: Omit<KnowledgeArtifactPanelProps, "compact" | "onOpenChange">) {
  if (!props.node) return null
  return (
    <KnowledgeNodeCard
      node={props.node}
      evidenceCards={props.evidenceCards}
      evidenceLoading={props.evidenceLoading}
      evidenceFailed={props.evidenceFailed}
      relatedNodes={props.relatedNodes}
      relationships={props.relationships}
      onTraceFromHere={props.onTraceFromHere}
    />
  )
}

export function KnowledgeArtifactPanel(props: KnowledgeArtifactPanelProps) {
  const { node, compact, onOpenChange } = props
  const { t } = useLocale()

  if (!compact) {
    return (
      <aside
        className={styles.desktopArtifactPanel}
        data-open={node ? "true" : "false"}
        aria-label={node
          ? t("knowledge.panel.aria", { name: node.entity.name })
          : t("knowledge.panel.genericAria")}
        aria-hidden={node ? undefined : true}
      >
        {node ? (
          <>
            <div className={styles.artifactPanelRail} aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon-lg"
              className={styles.artifactClose}
              onClick={() => onOpenChange(false)}
              aria-label={t("knowledge.panel.closeAria")}
            >
              <XIcon aria-hidden="true" />
            </Button>
            <ScrollArea className={styles.artifactScroll}>
              <ArtifactContents {...props} />
            </ScrollArea>
          </>
        ) : null}
      </aside>
    )
  }

  return (
    <Sheet open={Boolean(node)} onOpenChange={onOpenChange}>
      <SheetContent side="bottom" className={styles.artifactSheet}>
        <SheetHeader className="sr-only">
          <SheetTitle>{node
            ? t("knowledge.panel.aria", { name: node.entity.name })
            : t("knowledge.panel.title")}</SheetTitle>
          <SheetDescription>
            {t("knowledge.panel.description")}
          </SheetDescription>
        </SheetHeader>
        <ScrollArea className={styles.mobileArtifactScroll}>
          <ArtifactContents {...props} />
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}
