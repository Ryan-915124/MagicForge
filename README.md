# MagicForge

MagicForge is a self-hosted magic-performance intelligence system built with FastAPI, Next.js, PostgreSQL, Qdrant, and a governed Source → Claim → Evidence Card → Knowledge Node → Relationship pipeline. This repository is an Alpha: the public Demo is reproducible and read-only; Production remains operator-managed and fail closed.

## Demo Quick Start

Prerequisites:

- Docker Engine or Docker Desktop with Docker Compose v2
- Git
- Python 3.12.3 for the launcher safety and `doctor` checks; the Demo does not require third-party host packages
- approximately 4 GB of free memory and ports 3000, 8000, 5432, and 6333 available on loopback

On Windows with WSL, enable Docker Desktop integration for the distribution before continuing.

```bash
git clone https://github.com/Ryan-915124/MagicForge.git
cd MagicForge
./magicforge up demo
./magicforge doctor demo
```

Open <http://127.0.0.1:3000>. No login and no GLM API key are required. The interface should identify the runtime as `Demo`, `read-only`, and `Synthetic Demo Corpus`.

The Demo is rebuilt from the project-owned fixture in `data/demo/`. It contains 5 synthetic Sources, 10 Claims/Evidence Cards, 9 Knowledge Nodes, 10 Relationships, and 29 deterministic Qdrant projections. It does not read a private research run, initialize GLM, download an embedding model, or call an external AI service.

Stop the stack while preserving its named volumes:

```bash
./magicforge down demo
```

To deliberately delete only the local Demo volumes and prove a clean reconstruction:

```bash
./magicforge down demo --volumes --confirm delete-demo-volumes
./magicforge up demo
./magicforge doctor demo
```

`doctor` is read-only and exits nonzero if Docker, Compose, service health, migrations, Qdrant identity, point count, API readiness, frontend HTTP, profile identity, or the private-mount boundary is wrong.

## What the Demo contains

The public Demo exercises real product and domain paths:

- Magic Chat uses a deterministic offline response adapter plus retrieval from synthetic projections.
- Evidence Browser reads synthetic Evidence Cards and preserves evidence-origin labels.
- Knowledge Explorer receives non-empty nodes and relationships.
- Corpus Dashboard reads live runtime statistics; it never substitutes an old Bootstrap snapshot when the API is unavailable.
- Research Console exposes a Demo read model. The Production Research Console is explicitly unavailable in this Alpha.

Demo write surfaces remain disabled. The deterministic embedding adapter and offline language-model substitute are profile-scoped and cannot be selected by Production.

## Architecture

```text
Browser
  |
Next.js UI + same-origin BFF
  |
FastAPI API / RBAC / governance services
  |-------------------------|
PostgreSQL              Qdrant retrieval
  |                         |
Source -> Claim -> Evidence Card -> Knowledge Node -> Relationship
  |
Manifest -> authorization -> receipt -> Active Corpus
```

The codebase is a modular monolith. Production keeps named Source Approval, Claim Review, Mapping Review, Storage Authorization, Manifest/Receipt identity, Active Corpus binding, provenance, confidence, contradiction tracking, sensitivity filters, and the persistent-Qdrant write interlock.

Key directories:

```text
app/             FastAPI, runtime profiles, API routes
frontend/        Next.js product interface and BFF routes
knowledge/       ontology, evidence, manifests, projections, Demo fixture loader
persistence/     PostgreSQL models, repositories, transactions
research/        acquisition, extraction, review, and Bootstrap tooling
retrieval/       embedding and Qdrant adapters
security/        sessions, CSRF, RBAC, admin tooling
data/demo/       only publicly redistributable knowledge fixture
release/         positive public-source allowlist
scripts/         release audit, artifact builder, and operations helpers
```

## Development

The supported source toolchain is Python 3.12.3, Node.js 22.22.1, and npm 10.9.4.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock -r requirements-dev.lock
cd frontend
npm ci
cd ..
```

Copy the appropriate example without overwriting an existing local environment:

```bash
cp -n .env.development.example .env
cp -n frontend/.env.example frontend/.env.local
```

Development is a real `MAGICFORGE_PROFILE=development` runtime, not a disguised Demo. GLM is optional: an empty `GLM_API_KEY` leaves LLM-dependent functions unavailable without changing Production policy. A development Bootstrap corpus must be explicitly configured; the public repository does not ship a private corpus or populated vector store.

For the container topology:

```bash
./magicforge up development
./magicforge doctor development
```

This intentionally fails readiness until the Development corpus root, Manifest, Receipt, collection, and identity variables identify an available, separately licensed corpus. See [deployment](docs/DEPLOYMENT.md) and [database development](docs/database-development.md).

For direct host development, start PostgreSQL and Qdrant, then:

```bash
DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/magicforge' \
  .venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

Do not place passwords or provider keys in command arguments in shared environments. Prefer an untracked environment file or a secret manager.

### Initial administrator

Demo is anonymous and has no administrator. For an intended Development or Production database, create the first administrator through the hidden prompt:

```bash
MAGICFORGE_PROFILE=production \
DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/magicforge' \
.venv/bin/python -m security.admin_cli create-initial-admin \
  --username YOUR_ADMIN_USERNAME
```

The command refuses to bootstrap over an existing user table and never accepts a password as a command-line argument.

## Production

Production is not a promoted Demo and does not have a one-command public deployment. Use `.env.production.example` as a checklist, supply secrets outside Git, and review the [operator runbook](docs/production-governance-operator-runbook.md).

Production requires at least:

- `MAGICFORGE_PROFILE=production` and `MAGICFORGE_MODE=production`;
- a secret-managed PostgreSQL URL and current Alembic revision;
- TLS, `AUTH_COOKIE_SECURE=true`, and an exact HTTPS origin allowlist;
- provisioned users and least-privilege RBAC;
- named Source, Claim, Mapping, Manifest, and Storage decisions;
- an authorized Manifest, matching ingestion Receipt, and Active Corpus pointer;
- a Qdrant collection whose IDs, checksums, vector shape, and projection schema match that identity;
- backups, monitoring, incident response, and an operator-tested restore procedure.

Persistent Production Qdrant writes default to disabled. Enabling the global switch alone is insufficient: the exact Manifest ID, hash, collection, and point count must all match the separately reviewed authorization. Production never falls back to Demo, Bootstrap, or stale Dashboard data.

The Production services in `compose.yaml` are a fail-closed topology reference. They expose no PostgreSQL or Qdrant host ports, provide no default database password or Corpus, and will not become ready with incomplete configuration. Put a TLS reverse proxy and a secret-management layer in front of any real deployment.

## Backup and restore

The guarded Alpha helper currently targets only the local Demo profile:

```bash
./magicforge backup demo --output .magicforge/backups
./magicforge down demo
./magicforge restore demo \
  --input .magicforge/backups/BUNDLE_DIRECTORY \
  --confirm restore-demo
./magicforge up demo
./magicforge doctor demo
```

The bundle contains a PostgreSQL dump, one Qdrant snapshot, checksums, profile metadata, and point count. It is not a long-term cross-version backup format. See [operations](docs/OPERATIONS.md).

## Tests

```bash
.venv/bin/pytest -q
cd frontend
npm run typecheck
npm run lint
npm run test:security
npm run build
```

PostgreSQL migration integration requires `TEST_DATABASE_URL`. Real Qdrant integration requires `TEST_QDRANT_URL`. CI supplies both and treats those service-backed checks as required.

## Public release and data boundary

Never publish by copying the working directory or by running `git add .`. Private acquisition runs may remain on the maintainer's machine; the public artifact is assembled only from `release/public-allowlist.txt`.

```bash
./magicforge audit-public
./magicforge build-public-release
python scripts/audit_public_release.py --artifact dist/magicforge-public.zip
```

The builder creates a deterministic ZIP, `PUBLIC_RELEASE_MANIFEST.json`, and a `.sha256` sidecar. The audit rejects private paths, raw run trees, Qdrant/SQLite/snapshot/dump artifacts, source records that deny redistribution, symlinks, host-specific absolute paths, oversized files, and high-confidence secret patterns.

Only `data/demo/` is public knowledge data. Every Demo record is project-authored and explicitly marked `synthetic=true`, `self_authored=true`, and `redistribution_allowed=true`. Acquired papers, books, web captures, transcripts, provider responses, extracted content, reviews, and private Qdrant state are not distributed and receive no rights from the code license.

See [Data Boundary](docs/DATA_BOUNDARY.md), [data licensing](DATA_LICENSE.md), and the [release checklist](docs/RELEASE_CHECKLIST.md).

## License and Alpha status

The proposed software license is Apache-2.0. The proposed license for the project-owned synthetic Demo data is CC BY 4.0. A maintainer must formally confirm both choices, the copyright identity, and the attribution before the first public release. Third-party dependencies and model/provider assets retain their own terms; see [third-party notices](THIRD_PARTY_NOTICES.md).

Known deferred work is recorded in the ADRs for the [Production read-model snapshot](docs/adr/production-read-model-snapshot.md) and [background GLM worker](docs/adr/background-worker-boundary.md). Multi-tenancy and a microservice split are not part of this Alpha.
