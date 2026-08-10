## Summary

Describe the user-visible and architectural outcome.

## Verification

- [ ] Backend tests pass.
- [ ] Frontend checks pass when applicable.
- [ ] `./magicforge audit-public` passes.
- [ ] Production remains fail-closed.

## Data and security boundary

- [ ] No `.env`, credential, private corpus, raw source, extraction output, Qdrant storage, trace, or local absolute path is included.
- [ ] Synthetic Demo data is explicitly marked and contains no real citation or method secret.
- [ ] Any schema or governance change preserves provenance and human-review semantics.
