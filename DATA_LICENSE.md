# MagicForge data licensing

The Apache License in `LICENSE` covers MagicForge software and project-owned
documentation. It does **not** place research corpora or third-party source
material under the Apache License.

The public source artifact contains only the self-authored synthetic fixture in
`data/demo/`. Its machine-readable metadata must declare `synthetic=true`,
`self_authored=true`, and `redistribution_allowed=true`; the release audit fails
closed if any declaration is absent or false.

The self-authored synthetic fixture in `data/demo/` is licensed under Creative
Commons Attribution 4.0 International (`CC BY 4.0`). Copyright 2026
Ryan915124. The official license text is
<https://creativecommons.org/licenses/by/4.0/legalcode>. Distribution under
that license requires appropriate credit, a link to the license, and an
indication of changes. Recommended attribution:

> MagicForge Synthetic Demo Corpus, Copyright 2026 Ryan915124, licensed under
> CC BY 4.0.

This data license does not alter the Apache-2.0 license for MagicForge code and
project-owned documentation.

The artifact contains no acquired papers, books, website captures, transcripts,
provider payloads, private review runs, Qdrant snapshots, or production database
contents. No redistribution right is implied for those materials, their
excerpts, or their derived records.

A future non-demo dataset may be distributed only when every included item carries an
explicit, compatible license and the dataset release states its own terms. DOI,
URL, citation, or public accessibility alone is not redistribution permission.
Source-specific attribution, provenance, sensitivity, and human-review rules
continue to apply.

Third-party names, quotations, and source text are never synthetic merely
because they appear in a generated file.
