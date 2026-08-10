# MagicForge Operations

## Routine diagnostics

```bash
./magicforge doctor demo
./magicforge logs demo
```

Doctor is read-only and returns nonzero when a required check fails. It verifies Docker/Compose availability, the requested profile, service health, PostgreSQL, Alembic state, Qdrant, Demo collection identity and point count, API liveness/readiness, frontend HTTP, read-only Corpus identity, and absence of private host mounts.

Use `./magicforge doctor demo --static` when the Docker daemon is unavailable. Static Doctor uses either the project virtual environment or the Docker Compose CLI to validate configuration and private-data boundaries, but it cannot claim runtime health.

## Start, stop, and restart

```bash
./magicforge up demo
./magicforge down demo
./magicforge up demo
```

Ordinary `down` preserves named volumes. Do not manually remove PostgreSQL or Qdrant volumes while an environment matters.

## Logs

```bash
./magicforge logs demo
./magicforge logs demo api
```

Sanitize logs before sharing them. Logs must not be used to transport source bodies, credentials, session cookies, CSRF values, or operational magic details.

## Backup

The current guarded helper supports the local Demo profile only. Backups are written only to an explicit ignored directory:

```bash
./magicforge backup demo --output .magicforge/backups
```

The bundle captures PostgreSQL, the Demo Qdrant collection snapshot, and a metadata manifest. A backup from this Alpha is not a permanent cross-version interchange format.

For Development or Production, record the application version, migration revision, Corpus/Manifest/Receipt identities, Qdrant version, and restore test alongside every backup. Establish an external encrypted retention policy; a local bundle is not a disaster-recovery strategy.

## Restore

Restore is intentionally guarded. Stop API and Web first, verify the profile and bundle, then provide the exact confirmation token:

```bash
./magicforge down demo
./magicforge restore demo \
  --input .magicforge/backups/BUNDLE_DIRECTORY \
  --confirm restore-demo
./magicforge up demo
./magicforge doctor demo
```

Never restore a Demo bundle into Production. The restore helper rejects unknown profiles and mismatched metadata, but operators remain responsible for selecting the correct environment.

## Production incident posture

- Treat `database_not_ready`, stale Corpus identity, missing authorization, Receipt mismatch, and Qdrant checksum mismatch as safety boundaries, not errors to bypass.
- Keep persistent Qdrant writes disabled during investigation.
- Preserve audit rows and immutable review history.
- Capture sanitized request/correlation identifiers and component health.
- Restore service only after SQL, Manifest, Receipt, Active Corpus, and Qdrant identities agree.

## Known Alpha limits

- Production Research Console is explicitly unavailable in this Alpha.
- Production read-model snapshot optimization is deferred; see `docs/adr/production-read-model-snapshot.md`.
- GLM extraction still belongs to the API process; see `docs/adr/background-worker-boundary.md`.
- Automated reconciliation, hosted monitoring, MFA, and multi-tenant isolation are not included.
