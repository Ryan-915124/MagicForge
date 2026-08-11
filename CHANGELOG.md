# Changelog

All notable changes to MagicForge are documented here. Public tagged releases
follow Semantic Versioning. Internal architecture milestones are recorded
separately and do not define the public package version.

## [Unreleased]

## [0.1.0-alpha.3] - 2026-08-11

### Changed

- Unified the public Demo API and Doctor on the same isolated Qdrant collection
  while preserving deterministic offline retrieval and read-only mutation gates.
- Isolated Demo and Development Compose projects so destructive Demo cleanup
  cannot remove another profile's named volumes.
- Added same-runner clean rebuild and guarded PostgreSQL/Qdrant backup-restore
  verification to CI, and made release artifacts wait for the complete CI gate.
- Removed the host-Python prerequisite from the Docker Demo quick start and
  strengthened public-artifact, trace, log, cache, backup, and corpus boundaries.

### Fixed

- Restored Qdrant snapshots with explicit snapshot priority and checksum
  verification, and bound backups to the application, migration, Manifest, and
  Receipt identities.
- Kept the governance control plane reachable before a Production corpus is
  activated while product readiness continues to fail closed.

## [0.1.0-alpha.2] - 2026-08-11

### Changed

- Formally adopted Apache-2.0 for MagicForge software and project-owned
  documentation.
- Formally adopted CC BY 4.0 for the self-authored synthetic Demo corpus.
- Recorded Copyright 2026 Ryan915124 and added verified repository-wide code
  ownership for `@Ryan-915124`.
- Aligned the public API, frontend package, and Compose image metadata with the
  Alpha 2 release identifier.
- Updated the security policy and release checklist for repeatable prereleases.

## [0.1.0-alpha.1] - 2026-08-11

### Added

- Public/private data-boundary enforcement and allowlist-built source artifacts.
- Self-contained synthetic Demo profile and reproducible startup workflow.
- Full-stack container orchestration, Doctor diagnostics, and release safety checks.
- GitHub CI, CodeQL, Dependabot, release workflow, and community documentation.

### Security

- Production governance remains fail-closed and cannot fall back to Demo data.
- Private research runs, vector stores, credentials, traces, outputs, and backups are excluded from public artifacts.

## Internal architecture milestones

### 0.3.0 — unreleased internal milestone

- Production governance, RBAC, immutable review history, manifests, receipts, corpus activation, and Qdrant write interlocks.
- Magic Chat, Evidence Browser, Knowledge Explorer, Corpus Dashboard, Research Console, and localization foundations.
