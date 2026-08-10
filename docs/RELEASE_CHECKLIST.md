# MagicForge Public Alpha Release Checklist

Do not publish from the working directory. Build the public artifact from the positive allowlist and validate the extracted artifact in a clean room.

## Maintainer decisions

- [x] Confirm project name, copyright holder (`Ryan915124`), and year (`2026`).
- [x] Formally adopt Apache-2.0 for source code.
- [x] Formally adopt CC BY 4.0 for the self-authored synthetic Demo data.
- [ ] Rotate the previously configured GLM API key in the provider console.
- [x] Verify CODEOWNERS with the GitHub login `@Ryan-915124`.

## Source boundary

- [ ] `.env` and all environment-local files are untracked.
- [ ] `research/runs`, source bodies, extraction outputs, review runs, Qdrant stores, SQLite, dumps, traces, output, screenshots, caches, and backups are untracked.
- [ ] `data/demo` contains only synthetic, self-authored, redistributable records.
- [ ] No symlink points into a private directory.
- [ ] No public candidate contains a user-specific absolute path.
- [ ] `./magicforge audit-public` passes in source mode.

## Verification

- [ ] Backend full tests pass.
- [ ] PostgreSQL migration integration runs without skip.
- [ ] Real Qdrant integration runs without skip.
- [ ] Frontend typecheck, lint, security tests, and Production build pass.
- [ ] Production fail-closed regression tests pass.
- [ ] Docker backend and frontend images build.
- [ ] Compose configuration validates.
- [ ] First clean Demo start and Doctor pass.
- [ ] Demo HTTP/Evidence/Knowledge/Graph smoke passes without a GLM key.
- [ ] `down --volumes --confirm delete-demo-volumes`, second clean start, and second Doctor pass.
- [ ] Backup/restore smoke passes for Demo or Development.

## Artifact proof

- [ ] `./magicforge build-public-release` succeeds.
- [ ] Extracted artifact passes the public audit and secret scan.
- [ ] Artifact hash matches its `.sha256` file.
- [ ] Clean-room copy starts without `.env`, `research/runs`, host Qdrant data, or private paths.
- [ ] Artifact size and file list are reviewed.

## GitHub

- [ ] Inspect `git status`, staged file list, cached diff, large files, and public audit.
- [ ] Never use `git add .` for the first public commit.
- [ ] Create the official repository without pushing private history.
- [ ] Enable Private Vulnerability Reporting.
- [ ] Enable secret scanning and push protection.
- [ ] Confirm Actions permissions and branch protection.
- [ ] Create the Alpha tag and release only after CI succeeds.

## Explicitly deferred

- [ ] Production immutable read snapshot remains P2.
- [ ] Independent GLM worker remains P2.
- [ ] Multi-tenancy and microservices remain out of scope.
