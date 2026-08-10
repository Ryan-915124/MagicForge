# Contributing to MagicForge

MagicForge accepts changes to its public code, synthetic Demo Corpus, tests, and documentation. Private research material, copyrighted source text, operational magic secrets, extraction outputs, and local vector stores must never be contributed.

## Before opening a change

1. Create a branch from `main`.
2. Use Python 3.12.3 and Node 22.22.1.
3. Install Python dependencies from `requirements.lock` and `requirements-dev.lock`.
4. Install frontend dependencies with `npm ci`.
5. Keep Production governance fail-closed. Demo behavior must remain isolated behind `MAGICFORGE_PROFILE=demo`.

## Required checks

```bash
pytest -q
cd frontend && npm run typecheck && npm run lint && npm run test:security && npm run build
cd .. && ./magicforge audit-public
```

Service-backed tests require PostgreSQL and Qdrant through the documented development environment. Pull requests must not make those tests silently skip in CI.

## Data boundary

Only self-authored synthetic records under `data/demo/` may be proposed for the public Demo Corpus. They must:

- declare `synthetic=true`, `self_authored=true`, and `redistribution_allowed=true`;
- use stable identifiers;
- avoid real DOI, ISBN, author, publication, or practitioner attribution;
- avoid executable method details and restricted material;
- remain useful without implying scientific validation.

Run `./magicforge audit-public` before every pull request. See `docs/DATA_BOUNDARY.md` for the complete policy.

## Security reports

Do not disclose vulnerabilities in a public issue. Follow `SECURITY.md`.

## Commit and review expectations

Keep changes bounded, preserve provenance and governance semantics, add regression tests, and document operator-visible changes in `CHANGELOG.md`. The project currently uses a modular monolith; contributions should not introduce a new service or infrastructure dependency without an accepted architecture decision record.
