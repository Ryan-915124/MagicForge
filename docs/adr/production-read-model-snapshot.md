# ADR: Immutable Production Read-Model Snapshot

- Status: Proposed
- Scope: Deferred P2 work; not implemented in the Self-hosted Alpha release sprint

## Context

The current Production request path revalidates the Active Corpus and governed projection too often. Its fail-closed guarantees are valuable, but full Manifest and database verification on ordinary reads scales with corpus size and can serialize concurrent requests.

## Decision direction

Production will eventually use an immutable, versioned in-memory read snapshot. This ADR does not authorize weakening Manifest, Receipt, RBAC, sensitivity, provenance, or Qdrant identity checks.

### Request path

1. Read the Active Corpus pointer and immutable version identifier.
2. Compare that identifier with the loaded snapshot in O(1).
3. Serve from the already validated snapshot when they match.
4. Fail closed with a stable error if no validated snapshot exists.

### Activation and background path

1. Observe a candidate Active Corpus version.
2. Verify authorization, Manifest and Receipt identities, schema, database rows, Qdrant collection, point counts, checksums, and sensitivity policy.
3. Build a new immutable snapshot without mutating the active snapshot.
4. Atomically replace the active reference only after every check succeeds.
5. Preserve the previous snapshot on failure and expose the failed transition to operators.

## State machine

```text
absent → building → validated → active → superseded
              ↘ failed
active + new pointer → building-next → active-next
                                 ↘ failed (active remains unchanged)
```

Only `validated` snapshots can become `active`. A revoked or unauthorized corpus moves to a fail-closed state; stale data is not silently served after a governance revocation.

## Concurrency

- Readers hold an immutable reference and do not take the build lock.
- One builder lease exists per corpus/version.
- Atomic reference replacement publishes a complete snapshot.
- Old snapshots remain alive until in-flight readers release them.

## Cache invalidation and rollback

The Active Corpus pointer and authorization/revocation generation form the cache key. Rollback activates a previously validated version through the same governance operation; it never rewrites a snapshot in place.

## Metrics

- active snapshot version and age;
- pointer-check latency;
- snapshot build duration and failures;
- Manifest/database/Qdrant verification durations;
- atomic-switch count;
- stale/revoked read refusals;
- memory footprint and retained snapshot count.

## Benchmark plan

Measure 1k, 10k, and 100k projections at 1, 10, and 50 concurrent readers. Compare request latency, SQL query count, lock wait, snapshot build time, and memory against the current model.

## Test plan

- identity and checksum mismatch fail closed;
- activation is atomic under concurrent readers;
- failed build preserves the previously authorized snapshot;
- revocation invalidates reads without serving stale content;
- process restart rebuilds from governed state;
- rollback uses a previously validated version;
- metrics contain identifiers but no sensitive payloads.
