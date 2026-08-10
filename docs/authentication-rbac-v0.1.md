# MagicForge Authentication and RBAC v0.1

This document describes the implemented Checkpoint 2 authentication boundary
and the Checkpoint 3 Source/Claim governance authorization surface. Mapping,
storage-manifest, ingestion, and corpus-activation APIs remain out of scope.

## Authentication model

- There is no public registration route.
- Passwords use Argon2id. New passwords must be 12–128 characters and cannot
  equal the username or email address.
- Sessions use independently generated 256-bit opaque tokens. PostgreSQL stores
  only SHA-256 token digests; raw tokens exist only at the HTTP boundary.
- Sessions have an absolute lifetime (12 hours by default), can be revoked, and
  are rejected when expired, revoked, associated with a disabled user, or older
  than a real password change.
- Authentication reloads active roles from the database. Administrative
  services independently reload the user, session, and roles inside their
  transaction before authorizing a mutation.
- Browser sessions use an HttpOnly session cookie and a separate CSRF cookie.
  Cookie mutations require an allowlisted `Origin`, matching cookie/header CSRF
  tokens, and a match against the persisted CSRF digest.
- API clients can request a Bearer session. The opaque token is returned once at
  login and is revocable; it is not a JWT.

Create the first administrator explicitly after applying migrations:

```bash
python -m security.admin_cli create-initial-admin --username <name> [--email <address>]
```

The command prompts twice without echoing the password. Automation must name an
explicit environment variable with `--password-env`; there is no password
command-line option, default account, or default password. The command is
permanently rejected after the first user exists.

## Implemented RBAC matrix

Permissions are additive when a user has multiple roles.

| Capability | reader | reviewer | operator | admin |
|---|---:|---:|---:|---:|
| Chat, analysis, creation, authorized corpus inspection | yes | yes | yes | yes |
| Source registration and immutable version submission | no | no | yes | no |
| Claim candidate submission | no | no | yes | no |
| Source review | no | yes | no | no |
| Claim review | no | yes | no | no |
| Mapping review | no | yes | no | no |
| Research Console | no | no | yes | yes |
| Manifest build/authorize/ingest | no | no | yes | no |
| Corpus activation | no | no | yes | no |
| User and role administration | no | no | no | yes |
| Session revocation and audit access | no | no | no | yes |

An operator is not implicitly a reviewer, and an administrator is not
implicitly an operator or reviewer. A user who needs both responsibilities must
receive both roles. Self-review prevention belongs to the persisted review
workflow: the Source and Claim services reject a reviewer who submitted the
same object, even when that user has both operator and reviewer roles.

## Retrieval authorization

Routes never accept clearance or sensitivity filters from clients. They derive
one immutable `RetrievalAuthorization` from the authenticated actor and pass it
through the application service to every retrieval channel.

| Principal | Sensitive levels | Maximum exposure |
|---|---|---|
| Production anonymous | none; request rejected | none |
| reader/operator/admin (normal) | public, controlled | general principle |
| reviewer | public, controlled, secret method | method detail |
| explicitly elevated admin | adds restricted | operational secret |
| explicit Bootstrap anonymous principal | public, controlled Bootstrap projections only | general principle |

Restricted access requires the explicit break-glass policy primitive and a
specific reason. No HTTP break-glass activation route is exposed in Checkpoint
2, so restricted content remains unreachable through the current API.

Qdrant and the projected read model both fail closed when authorization is
missing. Evidence Cards, nodes, and relationships are filtered before response
model serialization. Missing or malformed sensitivity metadata is denied.

## HTTP surface

```text
POST  /auth/login
POST  /auth/logout
GET   /auth/me
POST  /admin/users
PATCH /admin/users/{id}/roles
POST  /admin/sessions/{id}/revoke

POST  /sources
POST  /sources/{id}/versions
GET   /sources/{id}
POST  /sources/{id}/review
POST  /claims
GET   /review/claims
GET   /claims/{id}
POST  /claims/{id}/review
GET   /evidence/{id}/versions
```

All Source/Claim mutations require `Idempotency-Key`; cookie-authenticated
mutations also require the existing Origin and CSRF checks. Source and Claim
review details are limited to authorized reviewers and filtered by
the actor's sensitivity clearance. Product Evidence responses omit reviewer
identity, decision reason, private provenance, and non-authorized excerpts.

Authentication and administrator failures use stable `detail.code` and
`detail.message` payloads with HTTP 401, 403, 404, 409, 422, or 503 semantics.
Health endpoints remain public and never return credentials.

## Bootstrap compatibility

Anonymous product reads are available only when both conditions hold:

```text
MAGICFORGE_MODE=bootstrap
BOOTSTRAP_ALLOW_ANONYMOUS_READS=true
```

That principal may use the existing Chat, Analyze, Create, Knowledge, Evidence,
Stats, and read-only Research Console surfaces against the configured Bootstrap
corpus. It has no review, administration, ingestion, storage, or activation
permission. Supplying an invalid session never falls back to anonymous access.
When this compatibility mode is enabled, an unavailable optional Bootstrap
database does not block those anonymous reads; authentication and administrator
routes return 503 until PostgreSQL becomes ready.

`BOOTSTRAP_ALLOW_ANONYMOUS_READS=true` is rejected as invalid configuration in
Production. Production product routes require a live authenticated session.

## Configuration

See `.env.example` for session lifetime, cookie names, HTTPS cookie behavior,
allowed origins, and the explicit Bootstrap compatibility switch. Production
must set `AUTH_COOKIE_SECURE=true` behind HTTPS.
