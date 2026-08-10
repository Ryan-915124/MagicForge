# MagicForge Self-hosted Deployment

This document covers the public Self-hosted Alpha. The supported public quick-start target is the read-only synthetic Demo profile. Production governance remains a stricter operator-managed deployment and never inherits Demo defaults.

## Supported toolchain

- Docker Engine with Docker Compose v2
- For source development: Python 3.12.3 and Node.js 22.22.1
- Loopback ports 3000, 8000, 5432, and 6333 must be available unless overridden

## Demo

```bash
./magicforge up demo
./magicforge doctor demo
```

Open <http://127.0.0.1:3000>. Demo is anonymous, read-only, does not need a GLM API key, and uses only the self-authored synthetic records in `data/demo/`.

The Demo stack contains PostgreSQL, Qdrant, an Alembic migration job, an idempotent Demo seed job, FastAPI, and Next.js. Named volumes retain state across ordinary restarts.

```bash
./magicforge logs demo
./magicforge down demo
```

To prove clean reconstruction, use the explicitly destructive volume option only when Demo data may be discarded:

```bash
./magicforge down demo --volumes --confirm delete-demo-volumes
./magicforge up demo
./magicforge doctor demo
```

This never targets Production, but it does delete the selected local Demo Compose volumes.

## Development

Development uses the same service topology with source-oriented API and frontend targets. It is a real `development` profile and requires an explicitly configured, separately licensed Bootstrap corpus:

```bash
./magicforge up development
./magicforge doctor development
```

GLM remains optional. When `GLM_API_KEY` is absent, LLM-dependent development functions report unavailable rather than preventing the knowledge read surfaces from starting. Do not place credentials in committed files.

The public checkout does not populate the Development corpus volume. Until the corpus root, Manifest, Receipt, schema, collection, and Qdrant contents agree, the Development API intentionally remains unready.

For direct host development:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -r requirements-dev.lock
cd frontend && npm ci && cd ..
```

Run migrations before the API:

```bash
DATABASE_URL='postgresql+psycopg://...' .venv/bin/alembic upgrade head
DATABASE_URL='postgresql+psycopg://...' .venv/bin/uvicorn app.main:app --reload
```

Then start Next.js with `MAGICFORGE_API_URL=http://127.0.0.1:8000`.

## Production boundary

The convenience Compose profile does not turn Demo into Production. Production requires, at minimum:

- `MAGICFORGE_PROFILE=production` and `MAGICFORGE_MODE=production`;
- a unique high-entropy database credential and TLS at the public edge;
- `AUTH_COOKIE_SECURE=true` and an exact HTTPS origin allowlist;
- completed Alembic migrations and a separately provisioned initial administrator;
- named Source, Claim, Mapping, Manifest, and Storage decisions;
- an authorized Manifest, matching ingestion Receipt and Active Corpus pointer;
- a matching Qdrant collection and projection identity;
- persistent Qdrant writes disabled unless an operator separately confirms the exact Manifest fingerprint;
- backup, monitoring, access-control, and incident-response procedures.

Production will not read `data/demo/`, fall back to Bootstrap, or start from Demo credentials. Missing governance state remains a readiness failure.

Follow `docs/production-governance-operator-runbook.md` for the current Production workflow. The Alpha does not yet provide a one-command hosted Production deployment.

## Initial administrator

Run this only against the intended Development or Production database. The command prompts without echo and refuses to create an administrator when users already exist:

```bash
MAGICFORGE_PROFILE=production \
DATABASE_URL='postgresql+psycopg://...' \
.venv/bin/python -m security.admin_cli create-initial-admin \
  --username YOUR_ADMIN_USERNAME
```

Do not put the password in a command argument, Compose file, repository, or shell history.

## Build-context boundary

Backend and frontend images are built from explicit COPY lists and `.dockerignore`. The image context excludes `.env`, `research/runs`, raw/extracted source material, Qdrant stores, dumps, traces, outputs, backups, caches, and local release artifacts. `data/demo/` is the only public data subtree included.
