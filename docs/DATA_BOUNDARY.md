# MagicForge public/private data boundary

MagicForge source code and MagicForge research data have different release
boundaries. A public source release is assembled from a positive allowlist; it
is never made by copying the repository and then deleting known private files.

## Public source release

The public artifact may contain only files selected by
`release/public-allowlist.txt`. The builder rejects unknown allowlist syntax,
missing entries, symbolic links, oversized files, host-specific absolute paths,
high-confidence credentials, private database files, vector-store snapshots,
run outputs, and source records whose metadata denies redistribution.

Build and verify an artifact with:

```bash
python scripts/build_public_release.py
python scripts/audit_public_release.py /path/to/extracted/magicforge-public
```

The build writes a deterministic ZIP archive and a SHA-256 sidecar. The archive
also contains `PUBLIC_RELEASE_MANIFEST.json`, which records every included path,
size, and content digest. It deliberately contains no build time, machine name,
or source checkout path.

## Private material

The following remain outside every public artifact:

- discovery and extraction runs;
- acquired or exact source bodies, receipts, and provider payloads;
- Qdrant local storage, snapshots, and database dumps;
- SQLite files and local PostgreSQL data;
- screenshots and ad-hoc test output;
- credentials, local environment files, and host-specific paths;
- any material whose source rights do not explicitly permit redistribution.

`data/demo/` is the only public data namespace. Its corpus metadata must state
that the material is synthetic, self-authored, and redistributable. The auditor
rejects every other top-level data path and rejects a demo corpus that lacks any
of those three affirmative declarations.

These files may remain in a developer's working directory. They must not be
deleted or moved merely to create a release. The allowlist builder ignores them
and audits the isolated staging tree before producing an archive.

## Adding a public file

1. Confirm that the file is source code, documentation, or redistributable
   project-owned content.
2. Add the narrowest exact path or subtree to the allowlist.
3. Run the public-release tests and build the archive twice.
4. Confirm that both SHA-256 values are identical.
5. Review the generated manifest before publishing.

Do not weaken the scanner to admit a file that fails. Remove private content
from that public file or keep the file outside the allowlist.

## Licensing

`LICENSE` applies to MagicForge software and project-owned documentation.
It does not grant rights to third-party papers, books, websites, transcripts,
provider responses, or derived research corpora. See `DATA_LICENSE.md` and
`THIRD_PARTY_NOTICES.md`.
