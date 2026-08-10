"use client"

import { useEffect, useMemo, useState, type FormEvent } from "react"
import {
  ArchiveRestoreIcon,
  FingerprintIcon,
  LockKeyholeIcon,
  PackageCheckIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
} from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import type { AuthenticatedActor } from "@/lib/api/types"
import { MagicForgeApiError } from "@/lib/api/client"
import { governanceApi } from "@/lib/api/governance-client"
import type {
  CorpusVersionView,
  EligibleArtifactView,
  ManifestBuildCommand,
  SensitiveInformationLevel,
  StorageManifestSummaryView,
  StorageManifestView,
} from "@/lib/api/governance-types"

import {
  DetailValue,
  MutationConfirmation,
  StatusBadge,
  errorText,
  shortId,
} from "./governance-common"
import { useStableMutationKey } from "./use-stable-mutation-key"
import {
  boundedPageOffset,
  updateGovernanceUrl,
  useGovernanceSearchParams,
} from "./governance-url-state"
import { useUnsavedWarning } from "./use-unsaved-warning"
import styles from "./governance.module.css"

type Translator = ReturnType<typeof useLocale>["t"]

function artifactLabel(type: EligibleArtifactView["artifact_type"], t: Translator) {
  if (type === "evidence_card") return t("governance.term.evidenceCard")
  if (type === "knowledge_node") return t("governance.term.knowledgeNode")
  return t("governance.term.relationship")
}

const MANIFEST_PAGE_SIZE = 50
const MANIFEST_MAX_OFFSET = 9900
const MANIFEST_ITEM_PAGE_SIZE = 50

function sensitivityLabel(level: SensitiveInformationLevel, t: Translator) {
  if (level === "public") return t("governance.enum.public")
  if (level === "controlled") return t("governance.enum.controlled")
  if (level === "secret_method") return t("governance.enum.secretMethod")
  return t("governance.enum.restricted")
}

export function ReleaseDesk({
  actor,
  onDirtyChange,
}: {
  actor: AuthenticatedActor
  onDirtyChange: (dirty: boolean) => void
}) {
  const { t } = useLocale()
  const searchParams = useGovernanceSearchParams()
  const artifactOffset = boundedPageOffset(searchParams.get("eligible_offset"), 50, 9900)
  const manifestOffset = boundedPageOffset(
    searchParams.get("manifest_offset"),
    MANIFEST_PAGE_SIZE,
    MANIFEST_MAX_OFFSET
  )
  const manifestItemOffset = boundedPageOffset(searchParams.get("manifest_item_offset"), MANIFEST_ITEM_PAGE_SIZE)
  const selectedManifestId = searchParams.get("manifest")
  const selectedCorpusId = searchParams.get("corpus")
  const [artifacts, setArtifacts] = useState<EligibleArtifactView[]>([])
  const [manifests, setManifests] = useState<StorageManifestSummaryView[]>([])
  const [corpora, setCorpora] = useState<CorpusVersionView[]>([])
  const [activeCorpus, setActiveCorpus] = useState<CorpusVersionView | null>(null)
  const [manualSelections, setManualSelections] = useState<Map<string, EligibleArtifactView>>(() => new Map())
  const [reloadToken, setReloadToken] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [corpusId, setCorpusId] = useState("")
  const [collectionName, setCollectionName] = useState("magicforge_knowledge_v01")
  const [sensitivityLevels, setSensitivityLevels] = useState<Set<SensitiveInformationLevel>>(() => new Set(["public", "controlled"]))
  const [buildCommand, setBuildCommand] = useState<ManifestBuildCommand | null>(null)
  const [building, setBuilding] = useState(false)
  const [loadedManifest, setLoadedManifest] = useState<StorageManifestView | null>(null)
  const [failedManifestId, setFailedManifestId] = useState<string | null>(null)
  const [authorizationDraft, setAuthorizationDraft] = useState({ manifestId: "", reason: "" })
  const [authorizeOpen, setAuthorizeOpen] = useState(false)
  const [authorizing, setAuthorizing] = useState(false)
  const [activationDraft, setActivationDraft] = useState({ corpusId: "", reason: "" })
  const [activationConfirmOpen, setActivationConfirmOpen] = useState(false)
  const [activating, setActivating] = useState(false)
  const [pendingNavigation, setPendingNavigation] = useState<
    { kind: "manifest" | "corpus"; id: string } | null
  >(null)
  const selectedManifest = selectedManifestId && loadedManifest?.id === selectedManifestId
    ? loadedManifest
    : null
  const manifestLoading = Boolean(
    selectedManifestId
    && loadedManifest?.id !== selectedManifestId
    && failedManifestId !== selectedManifestId
  )
  const authorizationReason = authorizationDraft.manifestId === selectedManifestId
    ? authorizationDraft.reason
    : ""
  const activationTarget = useMemo(
    () => corpora.find((corpus) => corpus.corpus_id === selectedCorpusId && corpus.activation_state === "staged") ?? null,
    [corpora, selectedCorpusId]
  )
  const activationReason = activationDraft.corpusId === selectedCorpusId
    ? activationDraft.reason
    : ""
  const setAuthorizationReason = (reason: string) => {
    setAuthorizationDraft({ manifestId: selectedManifestId ?? "", reason })
  }
  const setActivationReason = (reason: string) => {
    setActivationDraft({ corpusId: selectedCorpusId ?? "", reason })
  }
  const buildKey = useStableMutationKey("manifest-build")
  const authorizeKey = useStableMutationKey(`manifest-authorize:${selectedManifest?.id || "none"}`)
  const activateKey = useStableMutationKey(`corpus-activate:${activationTarget?.corpus_id || "none"}`)
  const releaseDirty = manualSelections.size > 0
    || Boolean(corpusId.trim())
    || collectionName !== "magicforge_knowledge_v01"
    || [...sensitivityLevels].sort().join(",") !== "controlled,public"
    || Boolean(authorizationReason.trim())
    || Boolean(activationReason.trim())

  useUnsavedWarning(releaseDirty, t("governance.unsaved.browserWarning"))

  useEffect(() => {
    onDirtyChange(releaseDirty)
  }, [onDirtyChange, releaseDirty])

  useEffect(() => () => onDirtyChange(false), [onDirtyChange])

  useEffect(() => {
    let active = true
    Promise.all([
      governanceApi.eligibleArtifacts(undefined, artifactOffset, 50),
      governanceApi.manifests(undefined, manifestOffset, MANIFEST_PAGE_SIZE),
      governanceApi.corpora(),
      governanceApi.activeCorpus().catch((cause) => {
        if (cause instanceof MagicForgeApiError && cause.status === 404) return null
        throw cause
      }),
    ]).then(
      ([eligiblePage, manifestPage, corpusResponse, activeVersion]) => {
        if (!active) return
        setArtifacts(eligiblePage.items)
        setManifests(manifestPage.items)
        setCorpora(corpusResponse.items)
        setActiveCorpus(activeVersion)
        setLoading(false)
      },
      (cause: unknown) => {
        if (!active) return
        setError(errorText(cause, t("governance.error.generic")))
        setLoading(false)
      }
    )
    return () => { active = false }
  }, [artifactOffset, manifestOffset, reloadToken, t])

  useEffect(() => {
    if (!selectedManifestId) return
    if (selectedManifest?.id === selectedManifestId) return
    let active = true
    governanceApi.manifest(selectedManifestId).then(
      (manifest) => {
        if (!active) return
        setLoadedManifest(manifest)
        setFailedManifestId(null)
        setError(null)
      },
      (cause: unknown) => {
        if (!active) return
        setLoadedManifest(null)
        setFailedManifestId(selectedManifestId)
        setError(errorText(cause, t("governance.error.generic")))
      }
    )
    return () => { active = false }
  }, [reloadToken, selectedManifest?.id, selectedManifestId, t])

  useEffect(() => {
    const rawEligibleOffset = searchParams.get("eligible_offset")
    const rawManifestOffset = searchParams.get("manifest_offset")
    const patch: Record<string, number | null> = {}
    if (rawEligibleOffset && rawEligibleOffset !== String(artifactOffset)) {
      patch.eligible_offset = artifactOffset || null
    }
    if (rawManifestOffset && rawManifestOffset !== String(manifestOffset)) {
      patch.manifest_offset = manifestOffset || null
    }
    if (Object.keys(patch).length > 0) updateGovernanceUrl(patch, "replace")
  }, [artifactOffset, manifestOffset, searchParams])

  useEffect(() => {
    if (!selectedManifest) return
    const maximumOffset = Math.floor(
      Math.max(0, selectedManifest.items.length - 1) / MANIFEST_ITEM_PAGE_SIZE
    ) * MANIFEST_ITEM_PAGE_SIZE
    if (manifestItemOffset > maximumOffset) {
      updateGovernanceUrl({ manifest_item_offset: maximumOffset || null }, "replace")
    }
  }, [manifestItemOffset, selectedManifest])

  const refresh = () => {
    setLoading(true)
    setError(null)
    setFailedManifestId(null)
    setReloadToken((value) => value + 1)
  }

  const requiredEvidenceIds = useMemo(() => {
    const required = new Set<string>()
    for (const artifact of manualSelections.values()) {
      if (artifact.artifact_type === "evidence_card") continue
      for (const evidenceId of artifact.supporting_evidence_version_ids) required.add(evidenceId)
    }
    return required
  }, [manualSelections])
  const selectedIds = useMemo(() => new Set([...manualSelections.keys(), ...requiredEvidenceIds]), [manualSelections, requiredEvidenceIds])
  const selectedArtifacts = useMemo(() => [...manualSelections.values()], [manualSelections])
  const selectedEvidenceIds = useMemo(() => new Set([
    ...selectedArtifacts.filter((item) => item.artifact_type === "evidence_card").map((item) => item.artifact_row_id),
    ...requiredEvidenceIds,
  ]), [requiredEvidenceIds, selectedArtifacts])
  const manifestHasMore = manifests.length === MANIFEST_PAGE_SIZE
    && manifestOffset < MANIFEST_MAX_OFFSET

  const toggleArtifact = (artifact: EligibleArtifactView, checked: boolean) => {
    setManualSelections((current) => {
      const next = new Map(current)
      if (checked) next.set(artifact.artifact_row_id, artifact)
      else next.delete(artifact.artifact_row_id)
      return next
    })
  }

  const prepareBuild = (event: FormEvent) => {
    event.preventDefault()
    const next: ManifestBuildCommand = {
      corpus_id: corpusId,
      collection_name: collectionName,
      evidence_version_ids: [...selectedEvidenceIds],
      knowledge_node_version_ids: selectedArtifacts.filter((item) => item.artifact_type === "knowledge_node").map((item) => item.artifact_row_id),
      relationship_assertion_ids: selectedArtifacts.filter((item) => item.artifact_type === "relationship").map((item) => item.artifact_row_id),
      authorized_sensitive_levels: [...sensitivityLevels],
    }
    setBuildCommand(next)
  }

  const buildManifest = async () => {
    if (!buildCommand) return
    setBuilding(true); setError(null)
    try {
      const manifest = await governanceApi.buildManifest(buildCommand, buildKey.keyFor(buildCommand))
      buildKey.clear(); setBuildCommand(null); setLoadedManifest(manifest); setManualSelections(new Map())
      setCorpusId("")
      setCollectionName("magicforge_knowledge_v01")
      setSensitivityLevels(new Set(["public", "controlled"]))
      updateGovernanceUrl({ manifest: manifest.id, manifest_item_offset: null }, "push")
      refresh()
    } catch (cause) { setError(errorText(cause, t("governance.error.generic"))); setBuildCommand(null) }
    finally { setBuilding(false) }
  }

  const inspectManifest = (id: string) => {
    if (authorizationReason.trim() && id !== selectedManifestId) {
      setPendingNavigation({ kind: "manifest", id })
      return
    }
    updateGovernanceUrl({ manifest: id, manifest_item_offset: null }, "push")
  }

  const prepareActivation = (corpus: CorpusVersionView) => {
    if (activationReason.trim() && corpus.corpus_id !== activationTarget?.corpus_id) {
      setPendingNavigation({ kind: "corpus", id: corpus.corpus_id })
      return
    }
    setActivationDraft({ corpusId: corpus.corpus_id, reason: "" })
    updateGovernanceUrl({ corpus: corpus.corpus_id }, "push")
  }

  const discardAndNavigate = () => {
    const target = pendingNavigation
    setPendingNavigation(null)
    if (!target) return
    if (target.kind === "manifest") {
      setAuthorizationDraft({ manifestId: target.id, reason: "" })
      updateGovernanceUrl({ manifest: target.id, manifest_item_offset: null }, "push")
      return
    }
    const corpus = corpora.find((item) => item.corpus_id === target.id)
    if (!corpus) return
    setActivationDraft({ corpusId: corpus.corpus_id, reason: "" })
    updateGovernanceUrl({ corpus: corpus.corpus_id }, "push")
  }

  const authorizeManifest = async () => {
    if (!selectedManifest) return
    setAuthorizing(true); setError(null)
    const payload = { reason: authorizationReason }
    try {
      const manifest = await governanceApi.authorizeManifest(selectedManifest.id, authorizationReason, authorizeKey.keyFor(payload))
      authorizeKey.clear(); setLoadedManifest(manifest); setAuthorizationReason(""); setAuthorizeOpen(false)
      refresh()
    } catch (cause) { setError(errorText(cause, t("governance.error.generic"))); setAuthorizeOpen(false) }
    finally { setAuthorizing(false) }
  }

  const activateCorpus = async () => {
    if (!activationTarget) return
    setActivating(true); setError(null)
    const payload = { runtime_scope: "production", reason: activationReason }
    try {
      await governanceApi.activateCorpus(activationTarget.corpus_id, activationReason, activateKey.keyFor(payload))
      activateKey.clear(); setActivationReason(""); setActivationConfirmOpen(false)
      updateGovernanceUrl({ corpus: null }, "replace")
      refresh()
    } catch (cause) { setError(errorText(cause, t("governance.error.generic"))) }
    finally { setActivating(false) }
  }

  const toggleSensitivity = (level: SensitiveInformationLevel, checked: boolean) => {
    setSensitivityLevels((current) => {
      const next = new Set(current)
      if (checked) next.add(level); else next.delete(level)
      return next
    })
  }

  if (loading) {
    return (
      <div className="grid gap-3 p-2" role="status" aria-live="polite" aria-busy="true">
        <span className="sr-only">{t("governance.loading.release")}</span>
        <Skeleton className="h-12 w-1/2" aria-hidden="true" />
        <Skeleton className="h-80 w-full" aria-hidden="true" />
        <Skeleton className="h-52 w-full" aria-hidden="true" />
      </div>
    )
  }

  return (
    <section aria-labelledby="release-desk-title">
      <div className={styles.sectionHeader}>
        <div>
          <p className={styles.eyebrow}>{t("governance.release.eyebrow")}</p>
          <h2 id="release-desk-title" className={styles.sectionTitle}>{t("governance.release.title")}</h2>
          <p className={styles.sectionDescription}>{t("governance.release.description")}</p>
        </div>
        <div className={styles.toolbar}><span className={`${styles.mono} ${styles.muted}`}>{actor.username}</span><Button variant="outline" onClick={refresh}><RefreshCwIcon aria-hidden="true" /> {t("governance.action.refresh")}</Button></div>
      </div>

      <div aria-live="polite">{error && <Alert variant="destructive" className={styles.error}><AlertTitle>{t("governance.error.operation")}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}</div>

      <div className={styles.releaseGrid}>
        <div className={styles.artifactVault}>
          <div className={styles.panelHeader}>
            <div className={styles.sheetMeta}>
              <div><p className={styles.microLabel}>{t("governance.release.eligibleRegister")}</p><h3 className={styles.sectionTitle}>{t("governance.release.approvedArtifacts")}</h3></div>
              <Badge variant="outline">{t("governance.release.eligibleCount", { count: artifacts.length })}</Badge>
            </div>
          </div>
          <div className={`${styles.panelBody} ${styles.artifactList}`}>
            {artifacts.length === 0 ? <div className={styles.empty}><div><ArchiveRestoreIcon className="mx-auto mb-3 size-5" aria-hidden="true" /><strong>{t("governance.release.noArtifacts")}</strong>{t("governance.release.noArtifactsDescription")}</div></div> : artifacts.map((artifact) => {
              const required = artifact.artifact_type === "evidence_card" && requiredEvidenceIds.has(artifact.artifact_row_id)
              return (
                <label className={styles.artifactRow} key={artifact.artifact_row_id}>
                  <Checkbox
                    checked={selectedIds.has(artifact.artifact_row_id)}
                    disabled={required}
                    onCheckedChange={(checked) => toggleArtifact(artifact, Boolean(checked))}
                    aria-label={`${artifactLabel(artifact.artifact_type, t)}: ${artifact.subject}`}
                  />
                  <span>
                    <strong className={styles.artifactTitle}>{artifact.subject}</strong>
                    <span className={`${styles.mono} ${styles.muted}`}>{artifactLabel(artifact.artifact_type, t)} · v{artifact.artifact_version} · {shortId(artifact.artifact_row_id)}</span>
                    {artifact.supporting_evidence_version_ids.length > 0 && <span className={styles.dependency}>{t("governance.release.evidenceDependencies", { count: artifact.supporting_evidence_version_ids.length })}</span>}
                    {required && <span className={styles.dependency}>{t("governance.release.autoDependency")}</span>}
                  </span>
                  <Badge variant="outline">{sensitivityLabel(artifact.sensitivity, t)}</Badge>
                </label>
              )
            })}
          </div>
          <div className={`${styles.actionRow} ${styles.panelBody}`}>
            <Button variant="outline" size="sm" disabled={artifactOffset === 0} onClick={() => { setLoading(true); updateGovernanceUrl({ eligible_offset: Math.max(0, artifactOffset - 50) }, "push") }}>{t("governance.action.previous")}</Button>
            <span className={`${styles.mono} ${styles.muted}`}>{artifacts.length ? `${artifactOffset + 1}–${artifactOffset + artifacts.length}` : "0"}</span>
            <Button variant="outline" size="sm" disabled={artifacts.length < 50 || artifactOffset >= 9900} onClick={() => { setLoading(true); updateGovernanceUrl({ eligible_offset: artifactOffset + 50 }, "push") }}>{t("governance.action.next")}</Button>
          </div>
        </div>

        <div className={styles.releasePanel}>
          <div className={styles.panelHeader}><p className={styles.microLabel}>{t("governance.release.manifestPress")}</p><h3 className={styles.sectionTitle}>{t("governance.term.manifest")}</h3></div>
          <form name="manifestBuild" autoComplete="off" className={`${styles.panelBody} ${styles.releaseForm}`} onSubmit={prepareBuild}>
            <Field><FieldLabel htmlFor="manifest-corpus" className={styles.fieldLabel}>{t("governance.field.corpusId")}</FieldLabel><div className={styles.toolbar}><Input id="manifest-corpus" name="corpusId" autoComplete="off" className={styles.input} value={corpusId} onChange={(event) => setCorpusId(event.target.value)} placeholder={t("governance.field.uuid")} required /><Button type="button" variant="outline" onClick={() => setCorpusId(crypto.randomUUID())}>{t("governance.action.generate")}</Button></div></Field>
            <Field><FieldLabel htmlFor="manifest-collection" className={styles.fieldLabel}>{t("governance.field.qdrantCollection")}</FieldLabel><Input id="manifest-collection" name="collectionName" autoComplete="off" className={styles.input} value={collectionName} onChange={(event) => setCollectionName(event.target.value)} required /><FieldDescription>{t("governance.release.collectionWarning")}</FieldDescription></Field>
            <fieldset><legend className={styles.fieldLabel}>{t("governance.release.authorizedSensitivity")}</legend><div className={`${styles.sensitivityList} mt-2`}>{(["public", "controlled", "secret_method", "restricted"] as SensitiveInformationLevel[]).map((level) => <label className={styles.checkLabel} key={level}><Checkbox checked={sensitivityLevels.has(level)} onCheckedChange={(checked) => toggleSensitivity(level, Boolean(checked))} />{sensitivityLabel(level, t)}</label>)}</div></fieldset>
            <div className={styles.packetGrid}>
              <DetailValue label={t("governance.term.evidenceCard")}>{selectedEvidenceIds.size}</DetailValue>
              <DetailValue label={t("governance.term.knowledgeNode")}>{selectedArtifacts.filter((item) => item.artifact_type === "knowledge_node").length}</DetailValue>
              <DetailValue label={t("governance.term.relationship")}>{selectedArtifacts.filter((item) => item.artifact_type === "relationship").length}</DetailValue>
            </div>
            <Button type="submit" disabled={selectedIds.size === 0 || sensitivityLevels.size === 0}><PackageCheckIcon aria-hidden="true" /> {t("governance.release.buildManifest")}</Button>
            <p className={`${styles.mono} ${styles.muted}`}>{t("governance.release.serverRevalidates")}</p>
          </form>
        </div>
      </div>

      <section className={`${styles.packet} mt-4`}>
        <div className={styles.packetHeader}><div><p className={styles.microLabel}>{t("governance.release.manifestRegister")}</p><h3 className={styles.sectionTitle}>{t("governance.release.releasePacket")}</h3></div>{manifestLoading && <span role="status" aria-live="polite">{t("governance.loading.manifest")}</span>}</div>
        <div className={styles.packetBody}>
          <Table className={styles.queueTable}>
            <TableHeader><TableRow><TableHead>{t("governance.term.manifest")}</TableHead><TableHead>{t("governance.term.collection")}</TableHead><TableHead>{t("governance.review.status")}</TableHead><TableHead>{t("governance.release.points")}</TableHead><TableHead className="text-right">{t("governance.review.inspect")}</TableHead></TableRow></TableHeader>
            <TableBody>{manifests.length === 0 ? <TableRow><TableCell colSpan={5}><div className={styles.empty}><div><strong>{t("governance.release.noManifests")}</strong>{t("governance.release.noManifestsDescription")}</div></div></TableCell></TableRow> : manifests.map((manifest) => <TableRow key={manifest.id}><TableCell className={styles.mono}>{shortId(manifest.id)}</TableCell><TableCell>{manifest.collection_name}</TableCell><TableCell><StatusBadge status={manifest.status} /></TableCell><TableCell>{manifest.expected_point_count}</TableCell><TableCell className="text-right"><Button variant="outline" size="sm" onClick={() => void inspectManifest(manifest.id)}>{t("governance.action.inspect")}</Button></TableCell></TableRow>)}</TableBody>
          </Table>
          <div className={`${styles.actionRow} mt-3`}>
            <Button
              variant="outline"
              size="sm"
              disabled={manifestOffset === 0}
              onClick={() => {
                setLoading(true)
                updateGovernanceUrl({ manifest_offset: Math.max(0, manifestOffset - MANIFEST_PAGE_SIZE) }, "push")
              }}
            >
              {t("governance.action.previous")}
            </Button>
            <span className={`${styles.mono} ${styles.muted}`}>
              {manifests.length ? `${manifestOffset + 1}–${manifestOffset + manifests.length}` : "0"}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={!manifestHasMore}
              onClick={() => {
                setLoading(true)
                updateGovernanceUrl({ manifest_offset: manifestOffset + MANIFEST_PAGE_SIZE }, "push")
              }}
            >
              {t("governance.action.next")}
            </Button>
          </div>
          <p className={`${styles.mono} ${styles.muted}`}>
            {t("governance.release.manifestRegister")} · {manifestOffset + manifests.length} / {MANIFEST_MAX_OFFSET + MANIFEST_PAGE_SIZE}
          </p>

          {selectedManifest ? (
            <div className={`${styles.sheetSection} mt-4`}>
              <div className={styles.packetHeader}>
                <div><p className={styles.microLabel}>{t("governance.release.exactPacket")}</p><h4 className={styles.sectionTitle}>{selectedManifest.collection_name}</h4></div>
                <StatusBadge status={selectedManifest.status} />
              </div>
              <div className={`${styles.packetGrid} mt-4`}>
                <DetailValue label={t("governance.field.manifestId")}>{selectedManifest.id}</DetailValue>
                <DetailValue label={t("governance.field.corpusId")}>{selectedManifest.corpus_id}</DetailValue>
                <DetailValue label={t("governance.release.points")}>{selectedManifest.expected_point_count}</DetailValue>
                <DetailValue label={t("governance.field.schema")}>{selectedManifest.schema_version}</DetailValue>
                <DetailValue label={t("governance.field.projection")}>{selectedManifest.projection_schema_version}</DetailValue>
                <DetailValue label={t("governance.release.items")}>{selectedManifest.items.length}</DetailValue>
              </div>
              <p className={`${styles.microLabel} mt-4`}>{t("governance.field.manifestHash")}</p><p className={styles.hash}>{selectedManifest.manifest_hash}</p>
              <div className={`${styles.packetGrid} mt-4`}>
                {(["evidence_card", "knowledge_node", "relationship"] as const).map((type) => <DetailValue key={type} label={artifactLabel(type, t)}>{selectedManifest.items.filter((item) => item.artifact_type === type).length}</DetailValue>)}
              </div>
              <h5 className={`${styles.microLabel} mt-4`}>{t("governance.release.exactItems")}</h5>
              <div className={styles.manifestItems}>
                <Table className={styles.queueTable}>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("governance.review.type")}</TableHead>
                      <TableHead>{t("governance.field.rowId")}</TableHead>
                      <TableHead>{t("governance.field.domainId")}</TableHead>
                      <TableHead>{t("governance.field.version")}</TableHead>
                      <TableHead>{t("governance.field.payloadChecksum")}</TableHead>
                      <TableHead>{t("governance.field.projectionChecksum")}</TableHead>
                      <TableHead>{t("governance.field.pointId")}</TableHead>
                      <TableHead>{t("governance.review.sensitivity")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {selectedManifest.items
                      .slice(manifestItemOffset, manifestItemOffset + MANIFEST_ITEM_PAGE_SIZE)
                      .map((item) => (
                      <TableRow key={`${item.artifact_type}:${item.artifact_row_id}`}>
                        <TableCell>{artifactLabel(item.artifact_type, t)}</TableCell>
                        <TableCell className={`${styles.mono} break-all`}>{item.artifact_row_id}</TableCell>
                        <TableCell className={`${styles.mono} break-all`}>{item.artifact_domain_id}</TableCell>
                        <TableCell>{item.artifact_version}</TableCell>
                        <TableCell className={`${styles.mono} break-all`}>{item.payload_checksum}</TableCell>
                        <TableCell className={`${styles.mono} break-all`}>{item.projection_checksum}</TableCell>
                        <TableCell className={`${styles.mono} break-all`}>{item.projection_point_id}</TableCell>
                        <TableCell>{sensitivityLabel(item.sensitivity, t)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <div className={`${styles.actionRow} mt-3`}>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={manifestItemOffset === 0}
                  onClick={() => updateGovernanceUrl({ manifest_item_offset: Math.max(0, manifestItemOffset - MANIFEST_ITEM_PAGE_SIZE) }, "push")}
                >
                  {t("governance.action.previous")}
                </Button>
                <span className={`${styles.mono} ${styles.muted}`}>
                  {selectedManifest.items.length
                    ? `${manifestItemOffset + 1}–${Math.min(manifestItemOffset + MANIFEST_ITEM_PAGE_SIZE, selectedManifest.items.length)} / ${selectedManifest.items.length}`
                    : "0"}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={manifestItemOffset + MANIFEST_ITEM_PAGE_SIZE >= selectedManifest.items.length}
                  onClick={() => updateGovernanceUrl({ manifest_item_offset: manifestItemOffset + MANIFEST_ITEM_PAGE_SIZE }, "push")}
                >
                  {t("governance.action.next")}
                </Button>
              </div>
              {selectedManifest.status === "pending" && (
                <div className="mt-4">
                  <Field><FieldLabel htmlFor="authorization-reason" className={styles.fieldLabel}>{t("governance.release.authorizationReason")}</FieldLabel><Textarea id="authorization-reason" name="manifestAuthorizationReason" autoComplete="off" className={styles.textarea} value={authorizationReason} onChange={(event) => setAuthorizationReason(event.target.value)} required /></Field>
                  <Button className="mt-3" onClick={() => setAuthorizeOpen(true)} disabled={!authorizationReason.trim()}><FingerprintIcon aria-hidden="true" /> {t("governance.release.authorizeManifest")}</Button>
                </div>
              )}
              <div className={styles.writeGate}><ShieldAlertIcon className="mb-2 size-5" aria-hidden="true" /><strong>{t("governance.release.persistentLock")}</strong><p className={styles.sectionDescription}>{t("governance.release.persistentLockDescription")}</p></div>
            </div>
          ) : <div className={styles.empty}><div><LockKeyholeIcon className="mx-auto mb-3 size-5" aria-hidden="true" /><strong>{t("governance.release.selectManifest")}</strong>{t("governance.release.selectManifestDescription")}</div></div>}
        </div>
      </section>

      <section className={`${styles.queuePanel} mt-4`}>
        <div className={styles.panelHeader}><div className={styles.sheetMeta}><div><p className={styles.microLabel}>{t("governance.release.corpusRegister")}</p><h3 className={styles.sectionTitle}>{t("governance.term.corpus")}</h3></div><Badge variant="outline">{activeCorpus ? `${t("governance.status.active")} · ${shortId(activeCorpus.corpus_id)}` : t("governance.release.noActiveCorpus")}</Badge></div></div>
        <Table className={styles.queueTable}>
          <TableHeader><TableRow><TableHead>{t("governance.field.corpusId")}</TableHead><TableHead>{t("governance.term.collection")}</TableHead><TableHead>{t("governance.review.status")}</TableHead><TableHead>{t("governance.field.projection")}</TableHead><TableHead className="text-right">{t("governance.release.activation")}</TableHead></TableRow></TableHeader>
          <TableBody>{corpora.length === 0 ? <TableRow><TableCell colSpan={5}><div className={styles.empty}><div><strong>{t("governance.release.noCorpora")}</strong>{t("governance.release.noCorporaDescription")}</div></div></TableCell></TableRow> : corpora.slice(0, 50).map((corpus) => <TableRow key={`${corpus.corpus_id}:${corpus.manifest_id}`}><TableCell className={styles.mono}>{shortId(corpus.corpus_id)}</TableCell><TableCell>{corpus.qdrant_collection}</TableCell><TableCell><StatusBadge status={corpus.activation_state} /></TableCell><TableCell>{corpus.projection_version} · {corpus.vector_size}d</TableCell><TableCell className="text-right">{corpus.activation_state === "staged" ? <Button variant="outline" size="sm" onClick={() => prepareActivation(corpus)}>{t("governance.release.prepareActivation")}</Button> : "—"}</TableCell></TableRow>)}</TableBody>
        </Table>
        {corpora.length > 50 && <p className={`${styles.panelBody} ${styles.mono} ${styles.muted}`}>{t("governance.release.corpusLimit", { count: corpora.length })}</p>}
        {activationTarget && <div className={styles.panelBody}><Field><FieldLabel htmlFor="activation-reason" className={styles.fieldLabel}>{t("governance.release.activationReason")}</FieldLabel><Textarea id="activation-reason" name="activationReason" autoComplete="off" className={styles.textarea} value={activationReason} onChange={(event) => setActivationReason(event.target.value)} /></Field><Button className="mt-3" disabled={!activationReason.trim()} onClick={() => setActivationConfirmOpen(true)}>{t("governance.release.activateCorpus")}</Button></div>}
      </section>

      <MutationConfirmation
        open={Boolean(buildCommand)}
        onOpenChange={(next) => !next && setBuildCommand(null)}
        title={t("governance.confirm.buildManifest")}
        description={t("governance.confirm.buildManifestDescription", { count: selectedIds.size, collection: buildCommand?.collection_name ?? collectionName })}
        details={buildCommand ? [
          { label: t("governance.field.corpusId"), value: buildCommand.corpus_id },
          { label: t("governance.term.collection"), value: buildCommand.collection_name },
          { label: t("governance.term.evidenceCard"), value: buildCommand.evidence_version_ids.length },
          { label: t("governance.term.knowledgeNode"), value: buildCommand.knowledge_node_version_ids.length },
          { label: t("governance.term.relationship"), value: buildCommand.relationship_assertion_ids.length },
          { label: t("governance.release.authorizedSensitivity"), value: buildCommand.authorized_sensitive_levels.join(", ") },
        ] : undefined}
        confirmLabel={t("governance.release.buildManifest")}
        pending={building}
        onConfirm={() => void buildManifest()}
      />
      <MutationConfirmation
        open={authorizeOpen}
        onOpenChange={setAuthorizeOpen}
        title={t("governance.confirm.authorizeManifest")}
        description={t("governance.confirm.authorizeManifestDescription", { count: selectedManifest?.expected_point_count ?? 0, collection: selectedManifest?.collection_name ?? "—" })}
        details={selectedManifest ? [
          { label: t("governance.field.manifestId"), value: selectedManifest.id },
          { label: t("governance.field.manifestHash"), value: selectedManifest.manifest_hash },
          { label: t("governance.field.corpusId"), value: selectedManifest.corpus_id },
          { label: t("governance.term.collection"), value: selectedManifest.collection_name },
          { label: t("governance.release.points"), value: selectedManifest.expected_point_count },
          { label: t("governance.release.authorizedSensitivity"), value: selectedManifest.authorized_sensitive_levels.join(", ") },
          { label: t("governance.field.reason"), value: authorizationReason },
        ] : undefined}
        confirmLabel={t("governance.release.authorizeManifest")}
        pending={authorizing}
        onConfirm={() => void authorizeManifest()}
      />
      <MutationConfirmation
        open={activationConfirmOpen}
        onOpenChange={setActivationConfirmOpen}
        title={t("governance.confirm.activateCorpus")}
        description={t("governance.confirm.activateCorpusDescription", { collection: activationTarget?.qdrant_collection ?? "—" })}
        details={activationTarget ? [
          { label: t("governance.field.corpusId"), value: activationTarget.corpus_id },
          { label: t("governance.field.manifestId"), value: activationTarget.manifest_id },
          { label: t("governance.term.collection"), value: activationTarget.qdrant_collection },
          { label: t("governance.field.projection"), value: `${activationTarget.projection_version} · ${activationTarget.vector_size}d · ${activationTarget.vector_distance}` },
          { label: t("governance.field.reason"), value: activationReason },
        ] : undefined}
        confirmLabel={t("governance.release.activateCorpus")}
        pending={activating}
        onConfirm={() => void activateCorpus()}
      />
      <MutationConfirmation
        open={Boolean(pendingNavigation)}
        onOpenChange={(open) => !open && setPendingNavigation(null)}
        title={t("governance.unsaved.title")}
        description={t("governance.unsaved.description")}
        confirmLabel={t("governance.unsaved.discard")}
        cancelLabel={t("governance.unsaved.keepEditing")}
        destructive
        pending={false}
        onConfirm={discardAndNavigate}
      />
    </section>
  )
}
