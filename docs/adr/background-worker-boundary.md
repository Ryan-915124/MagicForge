# ADR: Durable Background Worker Boundary

- Status: Proposed
- Scope: Deferred P2 work; not implemented in the Self-hosted Alpha release sprint

## Context

Production extraction has durable database records, but GLM work can still run within the FastAPI request lifecycle. Long operations, provider limits, restarts, and cancellation require ownership outside a browser request.

## Proposed responsibility split

### FastAPI

- authenticate and authorize job creation;
- validate the approved Source/permission chain;
- create an idempotent queued task;
- return task identity immediately;
- expose status, progress, result references, and cancellation requests;
- never perform the GLM extraction inline.

### Worker

- claim work through a bounded lease;
- call the existing GLM-only adapter;
- enforce provider concurrency and rate limits;
- renew heartbeat while working;
- write append-only attempts and proposal artifacts;
- retry classified transient failures with bounded backoff;
- honor cancellation between safe stages;
- recover work after lease expiration;
- never approve claims or write Production Qdrant directly.

### PostgreSQL task state

```text
queued → leased → running → succeeded
                    ├→ retry_wait → queued
                    ├→ cancelled
                    └→ failed
```

Each job stores attempt number, progress, result reference, sanitized error, lease owner, lease expiration, heartbeat, idempotency key, source-version identity, extraction policy version, and timestamps.

## Idempotency and crash recovery

The idempotency key binds source version, approved extraction permission, prompt/schema version, and requested operation. A worker may resume only after the previous lease expires. Artifact publication is transactional and duplicate successful results are rejected.

## Security and rate limiting

- Workers receive only the approved source scope.
- Logs never contain credentials or unrestricted source bodies.
- Per-provider and global concurrency limits apply backpressure.
- Authentication and human approval stay in the existing API/governance boundary.
- Production Qdrant write interlocks remain separate.

## Deferred platform choice

This ADR intentionally does not select Celery, Redis, Kafka, or another broker. The first implementation should evaluate a PostgreSQL lease queue against operational needs before adding infrastructure.

## Test plan

- duplicate submission is idempotent;
- only one live lease can execute;
- expired leases recover after process death;
- heartbeat prevents premature reclaim;
- transient errors retry within policy;
- permanent errors fail once classified;
- cancellation preserves audit history;
- provider limits are enforced;
- no worker path can approve or ingest knowledge.
