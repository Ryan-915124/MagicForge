# Changelog

All notable changes to MagicForge are documented here. Public tagged releases
follow Semantic Versioning. Internal architecture milestones are recorded
separately and do not define the public package version.

## [Unreleased]

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
