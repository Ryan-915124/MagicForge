import assert from "node:assert/strict"
import { access, readFile } from "node:fs/promises"
import test from "node:test"

import {
  cookieForwardingConfigFromEnv,
  isAllowedMagicForgeSetCookie,
  selectMagicForgeRequestCookies,
} from "../src/lib/api/cookie-forwarding.ts"
import { safeNextPath } from "../src/lib/auth/redirect.ts"
import { buildMagicForgeRequestHeaders } from "../src/lib/api/proxy-policy.ts"
import {
  citationVerificationReadyForApproval,
  extractionSupportsClaimApproval,
  extractionWithinRequestCeiling,
  permissionRequestRespectsAccess,
  requestAllowsApproval,
  scopeWithinRequestCeiling,
  storageWithinRequestCeiling,
} from "../src/features/governance/source-permission-policy.ts"

const secureConfig = cookieForwardingConfigFromEnv({ NODE_ENV: "production" })

test("request cookie forwarding keeps only the session and CSRF cookies", () => {
  assert.equal(
    selectMagicForgeRequestCookies(
      "magicforge_locale=zh-CN; magicforge_session=session-token; analytics=id; magicforge_csrf=csrf-token",
      secureConfig
    ),
    "magicforge_session=session-token; magicforge_csrf=csrf-token"
  )
})

test("BFF request policy forwards auth and mutation proofs but not proxy-spoofing headers", () => {
  const incoming = new Headers({
    Authorization: "Bearer bearer-token",
    Cookie: "magicforge_session=session-token; magicforge_csrf=csrf-token; locale=zh-CN",
    Origin: "https://magicforge.example",
    "Idempotency-Key": "operation:123",
    "X-CSRF-Token": "csrf-token",
    "X-Forwarded-For": "203.0.113.7",
    "X-Untrusted": "drop-me",
  })
  const selected = buildMagicForgeRequestHeaders(incoming, secureConfig, true)
  assert.equal(selected.get("authorization"), "Bearer bearer-token")
  assert.equal(selected.get("cookie"), "magicforge_session=session-token; magicforge_csrf=csrf-token")
  assert.equal(selected.get("origin"), "https://magicforge.example")
  assert.equal(selected.get("idempotency-key"), "operation:123")
  assert.equal(selected.get("x-csrf-token"), "csrf-token")
  assert.equal(selected.get("content-type"), "application/json")
  assert.equal(selected.get("x-forwarded-for"), null)
  assert.equal(selected.get("x-untrusted"), null)
})

test("Set-Cookie forwarding enforces cookie identity and security attributes", () => {
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; Max-Age=3600; Secure; HttpOnly; SameSite=lax",
      secureConfig
    ),
    true
  )
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_csrf=token; Path=/; Max-Age=3600; Secure; SameSite=lax",
      secureConfig
    ),
    true
  )
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; Secure; SameSite=lax",
      secureConfig
    ),
    false
  )
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Domain=example.com; Path=/; Secure; HttpOnly; SameSite=lax",
      secureConfig
    ),
    false
  )
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "unrelated=value; Path=/; Secure; HttpOnly; SameSite=lax",
      secureConfig
    ),
    false
  )
})

test("session deletion is accepted without requiring HttpOnly", () => {
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=; Path=/; Max-Age=0; Secure; SameSite=lax",
      secureConfig
    ),
    true
  )
})

test("future session expiry does not bypass the HttpOnly requirement", () => {
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; Expires=Wed, 09 Jun 2999 10:18:14 GMT; Secure; SameSite=lax",
      secureConfig
    ),
    false
  )
})

test("past session expiry is accepted as deletion without requiring HttpOnly", () => {
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure; SameSite=lax",
      secureConfig
    ),
    true
  )
})

test("invalid session expiry does not bypass the HttpOnly requirement", () => {
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; Expires=not-a-date; Secure; SameSite=lax",
      secureConfig
    ),
    false
  )
})

test("positive Max-Age prevents an expired Expires attribute from bypassing HttpOnly", () => {
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; Max-Age=3600; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure; SameSite=lax",
      secureConfig
    ),
    false
  )
})

test("local development can deliberately accept non-Secure loopback cookies", () => {
  const localConfig = cookieForwardingConfigFromEnv({
    NODE_ENV: "development",
    MAGICFORGE_REQUIRE_SECURE_COOKIES: "false",
  })
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; HttpOnly; SameSite=lax",
      localConfig
    ),
    true
  )
})

test("a local production server can explicitly accept non-Secure loopback auth cookies", () => {
  const localProductionConfig = cookieForwardingConfigFromEnv({
    NODE_ENV: "production",
    MAGICFORGE_REQUIRE_SECURE_COOKIES: "false",
  })
  assert.equal(localProductionConfig.requireSecure, false)
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_session=token; Path=/; HttpOnly; SameSite=lax",
      localProductionConfig
    ),
    true
  )
  assert.equal(
    isAllowedMagicForgeSetCookie(
      "magicforge_csrf=token; Path=/; SameSite=lax",
      localProductionConfig
    ),
    true
  )
})

test("post-login redirects remain same-origin paths", () => {
  assert.equal(safeNextPath("/governance?desk=review"), "/governance?desk=review")
  for (const unsafe of [
    "https://example.com",
    "//example.com",
    "/\\example.com",
    "/governance\nSet-Cookie:test",
  ]) {
    assert.equal(safeNextPath(unsafe), "/")
  }
})

test("the browser governance boundary exposes no persistent ingest route or client call", async () => {
  const ingestRoute = new URL(
    "../src/app/api/magicforge/governance/storage/manifests/[id]/ingest/route.ts",
    import.meta.url
  )
  await assert.rejects(access(ingestRoute), { code: "ENOENT" })
  const client = await readFile(
    new URL("../src/lib/api/governance-client.ts", import.meta.url),
    "utf8"
  )
  assert.doesNotMatch(client, /\/ingest\b|ingestManifest/)
})

test("Production Evidence stays sealed instead of presenting Bootstrap data as an active corpus", async () => {
  const archiveHeader = await readFile(
    new URL("../src/components/evidence/archive-header.tsx", import.meta.url),
    "utf8"
  )
  const evidenceBrowser = await readFile(
    new URL("../src/features/evidence/evidence-browser.tsx", import.meta.url),
    "utf8"
  )

  assert.doesNotMatch(archiveHeader, /bootstrap002Snapshot|bootstrap_v0[23]/)
  assert.match(evidenceBrowser, /active_corpus_not_configured/)
  assert.match(evidenceBrowser, /active_corpus_not_authorized/)
  assert.match(evidenceBrowser, /evidence-production-sealed/)
  assert.match(evidenceBrowser, /href="\/governance\?desk=review"/)
})

test("restricted Evidence never renders its claim or concept cross-reference", async () => {
  const dossier = await readFile(
    new URL("../src/components/evidence/evidence-card-view.tsx", import.meta.url),
    "utf8"
  )

  assert.match(dossier, /sealed \? t\("evidence\.dossier\.restrictedClaim"\) : card\.claim/)
  assert.match(dossier, /!sealed \? <section className="evidence-cross-reference"/)
})

test("Dashboard never substitutes a historical Bootstrap snapshot for failed live stats", async () => {
  const dashboard = await readFile(
    new URL("../src/features/dashboard/dashboard-overview.tsx", import.meta.url),
    "utf8"
  )
  const observation = await readFile(
    new URL("../src/features/dashboard/corpus-observation.ts", import.meta.url),
    "utf8"
  )

  assert.doesNotMatch(dashboard, /auditedCorpusObservation|bootstrap002Snapshot/)
  assert.doesNotMatch(observation, /bootstrap002Snapshot|bootstrap-002/)
  assert.match(dashboard, /No historical snapshot is substituted|waitingForLive/)
})

test("retrieval similarity is not relabeled as Evidence confidence", async () => {
  const evidencePanel = await readFile(
    new URL("../src/components/chat/evidence-panel.tsx", import.meta.url),
    "utf8"
  )

  assert.doesNotMatch(evidencePanel, /source\.confidence \?\? source\.score/)
  assert.match(evidencePanel, /source\.confidence == null/)
})

test("Demo access does not wait for an unavailable authentication dependency", async () => {
  const appShell = await readFile(
    new URL("../src/components/app-shell/app-shell.tsx", import.meta.url),
    "utf8"
  )

  const demoBoundary = appShell.indexOf("if (demoReadOnly) return children")
  const authLoadingBoundary = appShell.indexOf('if (authStatus === "loading")')
  const authUnavailableBoundary = appShell.indexOf('if (authStatus === "unavailable")')
  assert.ok(demoBoundary > -1)
  assert.ok(demoBoundary < authLoadingBoundary)
  assert.ok(demoBoundary < authUnavailableBoundary)
})

test("Production access fails closed when session roles cannot be verified", async () => {
  const appShell = await readFile(
    new URL("../src/components/app-shell/app-shell.tsx", import.meta.url),
    "utf8"
  )

  assert.match(appShell, /authStatus === "unavailable"/)
  assert.match(appShell, /shell\.accessSessionUnavailable/)
  assert.match(appShell, /refreshAuth/)
})

test("source links reject credential-bearing URLs and sanitize numeric readings", async () => {
  const evidencePanel = await readFile(
    new URL("../src/components/chat/evidence-panel.tsx", import.meta.url),
    "utf8"
  )

  assert.match(evidencePanel, /url\.username \|\| url\.password/)
  assert.match(evidencePanel, /Number\.isFinite\(source\.score\)/)
  assert.match(evidencePanel, /Number\.isFinite\(source\.confidence\)/)
})

const permissionRequest = Object.freeze({
  id: "8f23a91e-6636-4b62-9f59-69c26c1fc60f",
  source_version_id: "3d23626e-a5ea-4f8d-b84e-2d87d25531d3",
  sequence: 2,
  requested_extraction_permission: "selected_sections",
  requested_storage_permission: "derived_knowledge_only",
  requested_scope_locators: ["section:introduction", "section:discussion"],
  rights_basis: "Independent operator request pending reviewer determination.",
  rights_evidence: [],
  reason: "Request a bounded review.",
  submitted_by: "operator",
  actor_role_snapshot: ["operator"],
  request_checksum: "a".repeat(64),
  supersedes_request_id: null,
  created_at: "2026-08-08T08:00:00Z",
})

test("Source review permission choices cannot exceed the latest request", () => {
  assert.equal(extractionWithinRequestCeiling("none", "selected_sections"), true)
  assert.equal(extractionWithinRequestCeiling("metadata_only", "selected_sections"), true)
  assert.equal(extractionWithinRequestCeiling("selected_sections", "selected_sections"), true)
  assert.equal(extractionWithinRequestCeiling("full_text", "selected_sections"), false)
  assert.equal(storageWithinRequestCeiling("none", "derived_knowledge_only"), true)
  assert.equal(storageWithinRequestCeiling("derived_knowledge_only", "derived_knowledge_only"), true)
  assert.equal(storageWithinRequestCeiling("derived_with_short_excerpt", "derived_knowledge_only"), false)
})

test("selected-section approval remains a subset of the latest request", () => {
  assert.equal(
    scopeWithinRequestCeiling(["section:discussion"], "selected_sections", permissionRequest),
    true
  )
  assert.equal(
    scopeWithinRequestCeiling([" SECTION:DISCUSSION "], "selected_sections", permissionRequest),
    true
  )
  assert.equal(
    scopeWithinRequestCeiling(["section:methods"], "selected_sections", permissionRequest),
    false
  )
  assert.equal(scopeWithinRequestCeiling([], "selected_sections", permissionRequest), false)
  assert.equal(scopeWithinRequestCeiling([], "metadata_only", permissionRequest), true)
})

test("none/none requests block approval but remain bindable for rejection", () => {
  assert.equal(requestAllowsApproval(permissionRequest), true)
  assert.equal(requestAllowsApproval({
    ...permissionRequest,
    requested_extraction_permission: "metadata_only",
    requested_storage_permission: "none",
  }), false)
  assert.equal(requestAllowsApproval({
    ...permissionRequest,
    requested_extraction_permission: "none",
    requested_storage_permission: "none",
  }), false)
})

test("Source approval requires an actual claim-level extraction selection", () => {
  assert.equal(extractionSupportsClaimApproval("none"), false)
  assert.equal(extractionSupportsClaimApproval("metadata_only"), false)
  assert.equal(extractionSupportsClaimApproval("selected_sections"), true)
  assert.equal(extractionSupportsClaimApproval("full_text"), true)
})

test("Source approval requires complete matched citation verification", () => {
  const matched = {
    scope: "metadata",
    method: "canonical_locator",
    resolver_result: "matched",
    verified_identifier: "https://example.test/source",
    checked_locator: "https://example.test/source",
    resolver_name: "human-review",
    notes: "",
  }
  assert.equal(citationVerificationReadyForApproval(matched), true)
  assert.equal(citationVerificationReadyForApproval({ ...matched, resolver_result: "not_found" }), false)
  assert.equal(citationVerificationReadyForApproval({ ...matched, verified_identifier: "  " }), false)
  assert.equal(citationVerificationReadyForApproval({ ...matched, checked_locator: "" }), false)
  assert.equal(citationVerificationReadyForApproval({ ...matched, resolver_name: "" }), false)
})

test("short-excerpt requests require redistribution permission", () => {
  const excerptRequest = {
    ...permissionRequest,
    requested_storage_permission: "derived_with_short_excerpt",
  }
  assert.equal(permissionRequestRespectsAccess(excerptRequest, false), false)
  assert.equal(permissionRequestRespectsAccess(excerptRequest, true), true)
})

test("Source approve and reject commands bind the latest permission request", async () => {
  const sourceSheet = await readFile(
    new URL("../src/features/governance/review-sheets.tsx", import.meta.url),
    "utf8"
  )
  const bindings = sourceSheet.match(/source_permission_request_id:\s*permissionRequest\.id/g) ?? []
  assert.equal(bindings.length, 2)
  assert.match(sourceSheet, /command\.source_permission_request_id/)
  assert.match(
    sourceSheet,
    /disabled=\{!decision \|\| !permissionRequest \|\| !requestIdentityMatches \|\| \(decision === "approve" && approvalPreparationBlocked\)\}/
  )
  assert.match(sourceSheet, /version\.latest_permission_request_id === permissionRequest\.id/)
  assert.match(sourceSheet, /permissionRequest\.source_version_id === version\.id/)
})
