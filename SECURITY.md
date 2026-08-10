# Security Policy

## Supported versions

MagicForge is currently a self-hosted Alpha. Security fixes target the current
`main` branch and the latest GitHub prerelease; older prereleases are
unsupported once superseded. No compatibility or response-time guarantee is
offered yet.

## Reporting a vulnerability

Do not open a public issue for authentication, authorization, CSRF, secret exposure, private-corpus disclosure, Qdrant write-boundary, or supply-chain vulnerabilities.

GitHub Private Vulnerability Reporting is enabled. Submit reports through the
repository's **Security → Report a vulnerability** workflow. A public security
contact address has not been declared; this document intentionally does not
invent one.

Include a minimal reproduction that does not contain real credentials, private source text, copyrighted corpus material, or operational magic secrets.

## Security boundaries

- Production is fail-closed and requires its governed database, authorized Manifest/Receipt, Active Corpus, RBAC, and matching Qdrant projection.
- Demo is a separate, read-only profile backed only by self-authored synthetic data.
- Production must never fall back to Demo or Bootstrap data.
- Persistent Production Qdrant writes require explicit operator authorization and an exact manifest fingerprint.
- `.env`, local databases, Qdrant stores, traces, backups, research runs, and release work directories are not public assets.

## Operator responsibility

Self-hosters are responsible for TLS termination, host hardening, password policy, database and Qdrant access control, backups, upgrades, and restricting network exposure. Never reuse Demo credentials or configuration in Production.
