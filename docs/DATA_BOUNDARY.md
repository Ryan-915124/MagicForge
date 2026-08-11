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
python3 scripts/build_public_release.py --output dist/magicforge-public.zip
python3 scripts/audit_public_release.py --artifact dist/magicforge-public.zip
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

## Onboarding a private corpus

The public Alpha has no one-command arbitrary-document importer. That is a
safety boundary: a document becoming readable is not Source approval, Claim
approval, storage authorization, or redistribution permission.

Use the following staged procedure:

1. Keep the source body outside the repository, `data/demo/`, Docker build
   context, and public release tree. Restrict access and retain an immutable
   source checksum and locator.
2. Confirm that you have the rights to access, process, embed, store, and expose
   the material for the intended environment. Record source-specific limits;
   a DOI, URL, purchase, or public web page does not by itself grant those
   rights.
3. Configure Development or Production only through an untracked environment
   file or secret manager. Start from the applicable template without
   overwriting an existing file:

   ```bash
   cp -n .env.development.example .env
   chmod 600 .env
   ```

   Populate the corpus identity, Manifest/Receipt paths, remote Qdrant target,
   and authentication settings locally. Never add the resulting `.env` or
   private paths to the allowlist.
4. Register the Source and exact version, obtain human Source approval, extract
   bounded Claims, review Evidence Cards, and review entity and relationship
   mappings. The `/governance` interface and the API routes documented in
   [production-governance-backend.md](production-governance-backend.md#13-api-routes)
   expose these stages; none of them implies the next approval.
5. Build a Manifest only from currently eligible reviewed artifacts. A
   different authorized operator should inspect its exact ID, hash, collection,
   point count, projection schema, and sensitivity scope before authorizing
   ingestion. Persistent Production writes remain disabled unless the exact
   write-capability fields match that reviewed Manifest.
6. Verify the ingestion Receipt, activate the matching Corpus separately, then
   confirm API readiness and the product's displayed Corpus identity. Do not
   bypass a not-ready state or substitute Demo/Bootstrap data.

Compose Development intentionally does not import or bind-mount an arbitrary
host directory. Operators must provision an already governed corpus into the
configured private volume or use the direct-host Development topology described
in [DEPLOYMENT.md](DEPLOYMENT.md#development). If those prerequisites do not
exist, readiness is expected to fail closed.

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
