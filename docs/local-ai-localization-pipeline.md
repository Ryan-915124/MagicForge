# MagicForge Local AI Localization Pipeline

Status: implemented local-first proposal workflow
Canonical source locale: `en-US`
Target usability locale: `zh-CN`

## Purpose and boundary

This pipeline assists Chinese UI localization without turning MagicForge into a
fully translated consumer product. English remains canonical. Chinese is a
usability layer governed by:

- `localization/glossary.en-zh.yaml`;
- `localization/do-not-translate.yaml`;
- `localization/translation-policy.md`;
- `localization/brand-voice.md`.

MagicForge does not use an external translation platform, a second LLM
provider, or a human translation service. The optional model stage uses the
project's existing GLM adapter only.

“Local-first” does not mean that model inference is offline. Inventory,
classification, validation, and artifact creation run locally. `scan`,
`validate`, and the default `propose` planning mode make no model or network
request. Adding `--use-glm` explicitly sends a bounded UI-only request through
the official Z.AI GLM API, so that mode is not fully offline.

The current CLI creates plans, machine proposals, validation results, and review
queues. It has no apply or publish command. It never automatically changes the
runtime catalog, Translation Memory, glossary status, or an `approved` state.

## Implemented flow

```text
frontend/src/lib/i18n/messages.ts (English canonical catalog)
                         |
                         v
read-only TypeScript AST inventory
                         |
                         v
local glossary and do-not-translate classification
                         |
              +----------+----------+
              |                     |
              v                     v
      offline proposal plan   explicit --use-glm
                                    |
                                    v
                         Z.AI GLM machine candidates
              +---------------------+
              |
              v
deterministic validation and local machine proposal queue
              |
              v
verified multi-run assembly (later input wins; still not publishable)
                         |
                         v
selected semantic quality review (explicit --use-glm; still not publishable)
```

The TypeScript inventory reads both `enUS` and `zhCN` string-literal catalogs,
tracks `useLocale()` translator aliases and refs, statically resolves supported
conditional and mapping-based message keys, reports unresolved and unused keys,
checks placeholder parity, and lists possible hardcoded JSX text, expressions,
and visible attributes. It writes JSON to stdout and never edits source files.

Python then builds a `SourceUnit` for every selected English key. Each unit
contains its SHA-256 source hash, current target, classification, required
action, placeholders, protected terms, and source-code usage context.

## Requirements

- Run commands from the repository root.
- Use the project Python environment with dependencies from
  `requirements.txt`.
- Install the existing frontend dependencies so the inventory can load the
  bundled TypeScript compiler.
- Node.js must be available as `node`, or supplied with `--node`.
- Only `propose --use-glm` requires `GLM_API_KEY` and Z.AI connectivity. It also
  uses the existing `GLM_MODEL`, timeout, and retry settings from `.env`.

No localization-specific package or external translation-platform account is
required.

## CLI commands

The implemented command surface is:

```text
python -m localization.pipeline scan
python -m localization.pipeline propose
python -m localization.pipeline validate
python -m localization.pipeline assemble
python -m localization.pipeline quality-review
```

There is intentionally no `apply`, `publish`, `sync`, or Translation Memory
promotion command.

### 1. Fully offline scan

```bash
python -m localization.pipeline scan \
  --run-id localization-scan-002
```

This command:

1. runs the read-only Node/TypeScript AST inventory;
2. loads and cross-checks the local glossary and protection policy;
3. classifies all catalog units;
4. validates the current `zhCN` catalog deterministically;
5. writes a local run directory.

It does not initialize `GLMClient`, even if a GLM key is configured.

The inventory can also be run directly:

```bash
node frontend/scripts/localization-inventory.mjs > /tmp/magicforge-i18n-inventory.json
```

The shell redirection creates the file; the inventory script itself only emits
JSON to stdout.

### 2. Offline proposal planning

`propose` does not call GLM unless `--use-glm` is present. At least one selector
is required.

Plan one key:

```bash
python -m localization.pipeline propose \
  --key chat.composer.submit \
  --run-id localization-chat-submit-plan-001
```

Plan one or more catalog namespaces:

```bash
python -m localization.pipeline propose \
  --module chat \
  --module evidence \
  --run-id localization-chat-evidence-plan-001
```

Plan every unit whose governed action is ordinary or mixed UI translation:

```bash
python -m localization.pipeline propose \
  --all-normal-ui \
  --run-id localization-normal-ui-plan-001
```

Selectors are additive. Supported modules are `language`, `shell`, `chat`,
`evidence`, `knowledge`, `dashboard`, `research`, and `shared`.

Planning writes selected source units and a baseline audit, but it writes no
`proposals.jsonl` because no candidate has been generated.

### 3. Explicit GLM proposal generation

```bash
python -m localization.pipeline propose \
  --module chat \
  --run-id localization-chat-glm-001 \
  --batch-size 8 \
  --use-glm
```

`--use-glm` is the network and model-call boundary. It uses the existing Z.AI
GLM client and no other provider. `--batch-size` accepts 1–16 units; the default
is 8. Batches also have a fixed character budget.

The model receives only the bounded localization fields for selected UI catalog
units:

- message key, English source text, and source hash;
- a localization source in which protected variants are normalized to their
  canonical English spelling;
- current Chinese target;
- classification and governed action;
- placeholders and protected terms;
- canonical target terms with required minimum occurrence counts;
- source-code usage locations.

The localization prompt does not contain corpus documents, Evidence Card
claims, source excerpts, citations, paper or book titles, author or performer
records, Qdrant payloads, or credentials. Z.AI request authentication still
uses `GLM_API_KEY` outside the prompt body. Generated results remain
`machine_proposed`. The schema value `generated_by: local_ai` identifies this
local workflow; it does not claim that GLM inference happened offline.

### 4. Offline validation of a proposal file

```bash
python -m localization.pipeline validate \
  --input localization/runs/localization-chat-glm-001/proposals.jsonl \
  --run-id localization-chat-validation-001
```

This command makes no model request. It parses every JSONL record with strict
Pydantic models, rebuilds the current catalog and policy context, verifies the
proposal batch, and writes a separate validation run. The input proposal file
is not modified. An empty proposal file fails closed. The validation manifest
records the input SHA-256 and `validation_scope: supplied_proposal_records`:
validation proves consistency only for the records supplied in that file. It
does not prove that the file still contains every record from an earlier GLM
generation run.

### 5. Verified offline assembly

Large catalog generations may be split across module runs, with later
remediation runs replacing individual candidates. `assemble` combines only
completed, integrity-checked `propose` runs. Input order is meaningful: when a
key appears more than once, the candidate from the last `--input-run` wins.

```bash
python -m localization.pipeline assemble \
  --input-run localization/runs/localization-glm-language-001 \
  --input-run localization/runs/localization-glm-chat-001 \
  --input-run localization/runs/localization-glm-chat-terms-001 \
  --require-complete-catalog \
  --run-id localization-glm-candidates-001
```

Before accepting an input, assembly verifies:

- `manifest.json` declares `status: completed`, `command: propose`, successful
  deterministic validation, and GLM-only generation lineage;
- the input declares that runtime, Translation Memory, external translation
  platforms, and human translators were not used;
- the on-disk `proposals.jsonl` SHA-256 and byte count match the manifest;
- every record strictly satisfies `TranslationProposal` and has a unique key;
- every proposal key, English source text, and source hash still matches the
  current canonical catalog.

Only after those checks does the local assembler reattach trusted catalog
metadata such as protected terms and locale identity from the current
`SourceUnit`. It does not alter candidate wording, rationale, confidence,
provider, status, or generation origin. The final overlaid batch passes the
normal deterministic validator again.

`--require-complete-catalog` additionally requires exact key-set equality with
the current catalog. The final manifest records every upstream manifest and
proposal SHA-256, overlay order, replacement events, unique overridden keys,
and SHA-256 fingerprints for the pipeline implementation used for full-batch
validation.

Assembly makes no model request. Its output remains `machine_proposed` review
material; the command has no apply, publish, approve, promotion, or Translation
Memory write behavior.

### 6. Selected second-pass semantic quality review

Deterministic validation cannot identify every domain-sense error. For example,
`performance` may mean 表演 rather than 性能, a storage `receipt` is not a shop
收据, and a software `Smoke test` is not smoke imagery. `quality-review` performs
a bounded second GLM pass over an explicitly selected subset of an assembled
candidate run.

First create a fully offline plan for a curated list:

```bash
python -m localization.pipeline quality-review \
  --input-run localization/runs/localization-glm-candidates-001 \
  --keys-file localization/review/quality-review-002-keys.txt \
  --run-id localization-quality-review-plan-002
```

Only the explicit flag permits the model request:

```bash
python -m localization.pipeline quality-review \
  --input-run localization/runs/localization-glm-candidates-001 \
  --keys-file localization/review/quality-review-002-keys.txt \
  --run-id localization-quality-review-002 \
  --batch-size 8 \
  --use-glm
```

Selection options are additive:

- repeat `--key` for individual message keys;
- repeat `--keys-file` for newline-delimited key lists (blank lines and `#`
  comments are ignored);
- use `--changed-only` for candidates that differ from the current runtime
  target.

At least one selector is required. A curated key list is preferred over a broad
rewrite because most first-pass candidates are already suitable.

Before any model call, the command requires either a completed `assemble` run
or a completed, full-catalog `quality-review` run. For an assembly it reads
`proposals.jsonl`. For an iterative quality-review input it reads only
`final-proposals.jsonl`, never the previous selected-subset `proposals.jsonl`.
It verifies manifest status, GLM-only lineage, current canonical catalog
identity, strict proposal schemas, and recorded SHA-256 plus byte counts.

An iterative quality-review input must additionally declare
`full_catalog_overlay: true` and `final_validation_passed: true`; its
`final_proposal_count` must match the file, and both `final-proposals.jsonl` and
`final-validation-report.json` must match their manifest fingerprints. The
strict final validation report must be a PASS for the same proposal count, and
the final keys must equal the current catalog. The pipeline then validates the
entire input again under current policy before selecting any next-round keys.

The model receives only the selected English UI source, current runtime target,
latest candidate, policy classification, placeholders, protected terms, and
source-code locations.

Each structured decision is exactly one of:

- `keep`: no semantic issue found;
- `revise`: a changed candidate is supplied;
- `needs_human_review`: material ambiguity remains and the original candidate
  is preserved.

If GLM declares `revise` but returns text identical to the input candidate, the
local pipeline does not accept it as a revision and does not fail the remaining
batch. It deterministically downgrades the result to `needs_human_review`,
preserves the original candidate and issue types, records
`model_disposition: revise` plus
`normalization: unchanged_revision_conflict`, and appends the conflict to the
rationale. Run manifests and summaries count these fail-safe normalizations.

The same boundary applies when GLM declares `revise` but omits or returns an
empty `revised_candidate_chinese`. The structured response is allowed into the
trusted conversion layer, then downgraded to `needs_human_review` with
`normalization: missing_revision_candidate`. The original candidate and issue
types are preserved and the missing revision is recorded in the rationale.
Other contradictory combinations, such as `keep` carrying revised text or
`needs_human_review` carrying revised text, remain schema errors.

If a non-empty changed revision fails the deterministic validator, it is never
accepted and is never relabeled `keep`. The result is downgraded to
`needs_human_review` with `normalization: invalid_revision_policy`; the original
candidate and model issue types are preserved, applicable
`protected_term_risk` or `placeholder_risk` types are appended, and every
Validator error code is retained in `normalization_issue_codes` and the
rationale for auditability.

The raw GLM response schema permits rationale text up to 2,000 characters so a
verbose but otherwise valid review does not fail during parsing. Before any
decision or revised proposal is persisted, the trusted conversion layer
collapses whitespace and deterministically bounds rationale to 500 characters.
Ordinary truncation is marked with `[truncated]`. Fail-safe normalization
rationales reserve space for, and always preserve, their audit suffix within
the same 500-character limit.

Every `revise` result is rebuilt as a GLM/local-AI `machine_proposed`
`TranslationProposal` and must pass the normal deterministic validator. A
`keep` is not approval, and `needs_human_review` does not block artifact
creation. The command writes no runtime messages, Translation Memory entries,
or governance approval. In addition to the selected `proposals.jsonl`, a
successful review overlays its selected results onto the complete verified
input in canonical catalog order as `final-proposals.jsonl`. The entire unified
set is validated again in `final-validation-report.json`; this is a reviewable
v2 candidate package, not a publication artifact.

### Shared run options

All five commands accept:

- `--run-id`: lowercase letters, digits, dots, underscores, and hyphens, up to
  80 characters;
- `--output-root`: alternate run root; the default is `localization/runs`;
- `--resume`: reuse an existing run directory;
- `--node`: alternate Node.js executable for the AST inventory.

If `--run-id` is omitted, the CLI creates a UTC timestamped ID.

## Run artifacts

Each command writes under `localization/runs/<run-id>/` unless `--output-root`
is supplied.

| Artifact | Created by | Meaning |
| --- | --- | --- |
| `manifest.json` | all commands | Final run commit record: status, identity, source-catalog hash, policy version, command, unit count, explicit mutation/provider flags, and SHA-256 fingerprints of completed artifacts. |
| `inventory.json` | `scan` | Full AST catalog, usage, parity, and hardcoded-candidate inventory. |
| `inventory-summary.json` | `propose`, `validate`, `assemble`, `quality-review` | Compact inventory and catalog-validation summary. |
| `source-units.jsonl` | all commands | Governed English source units and current target context. |
| `hardcoded-candidates.jsonl` | all commands | Governed review hints for possible hardcoded UI text; never an automatic rewrite list. |
| `validation-report.json` | all commands | Deterministic errors, warnings, counts, and pass/fail result. |
| `summary.md` | all successful stages | Human-readable run summary and governance boundary. |
| `proposals.jsonl` | GLM `propose`, `validate`, `assemble`, `quality-review` | Strict machine proposal records, a validated supplied set, an integrity-checked overlay, or selected review results. In quality review, `keep`/`needs_human_review` preserve the input and `revise` contains the validated machine revision. |
| `review-decisions.jsonl` | GLM `quality-review` | Structured semantic decisions with issue types, rationale, confidence, input proposal hash, and any still-machine-proposed revision. |
| `final-proposals.jsonl` | GLM `quality-review` | Unified canonical-order candidate set with selected results overlaid onto the verified assembly input. |
| `final-validation-report.json` | GLM `quality-review` | Deterministic validation of the entire unified candidate set. |
| `review-queue.jsonl` | GLM `propose`, `validate`, `assemble`, `quality-review` | Proposal plus per-key validation findings and pending governance state. |
| `failure.json` | failed GLM generation or quality review | Local failure stage, type, time, and safe message; prompts and credentials are not intentionally recorded. |

Writes use a temporary file, `fsync`, and atomic replacement so a named
artifact is not left partially written. A run starts with `status: started`;
after every required data artifact has been written, `manifest.json` is written
last with `status: completed`. A GLM generation failure commits `status: failed`
and fingerprints `failure.json` plus the retained baseline artifacts.

`manifest.json` explicitly records `runtime_modified: false` and
`translation_memory_modified: false`. Locally produced `scan` and `propose`
runs also record `external_translation_platform_used: false` and
`human_translator_used: false`. A successful GLM run additionally records
`llm_invoked: true`, provider `GLM`, and the configured model name. Because an
arbitrary `validate --input` file has no trusted generation lineage, its
external-platform and human-translator fields are `null` and its provenance is
`supplied_input_unverified`.

## Deterministic gates

The pipeline fails closed on malformed policy assets, catalog parse errors,
duplicate or unknown keys, catalog or placeholder mismatch, invalid JSONL, and
proposal schema violations. Proposal validation also checks:

- exact source text and SHA-256 identity;
- one proposal for every selected key and no extra key;
- named-brace placeholder names and multiplicity, for example `{number}`;
- exact preservation of `MagicForge`, technology names, product names, and
  English-only professional terms;
- draft aliases and unapproved bilingual labels are not rendered;
- prohibited brand phrases are absent;
- `preserve_exact` and `preserve_english_only` actions are respected;
- machine output cannot use an `approved` status;
- required provider, generation origin, and proposal status form one of the
  implemented provenance combinations (`glm/local_ai/machine_proposed` or
  `deterministic/deterministic/machine_reviewed`).

The GLM response must match the selected keys exactly and in input order. It
does not return canonical source text or hashes; the trusted local caller
reattaches those fields from the selected `SourceUnit`, so the model cannot
rewrite source identity. Extra response fields are rejected.

Hardcoded candidates and unused keys are review findings, not automatic source
edits. Unresolved dynamic `MessageKey` references are reported separately, so
“unused” means that no statically resolved translator-call candidate was found.

## Failure handling and recovery

Exit behavior is:

- `0`: deterministic validation passed;
- `2`: known inventory, policy, artifact, proposal-contract, input, or
  validation failure;
- `3`: GLM configuration or Z.AI API failure.

An inventory or policy failure can occur before a run directory is created.
Correct the local source or governance asset and rerun with a new run ID.

Assembly verifies every upstream run and the complete overlay before creating
its output directory. A bad status, command, fingerprint, schema, stale source,
duplicate key, provenance declaration, or incomplete catalog therefore fails
without emitting a candidate run.

If GLM generation fails after a run starts, the directory retains its baseline
inventory, source units, validation report, summary inputs, and `failure.json`.
No proposal queue is published from the failed request. After correcting the
API key or connectivity, either:

1. rerun with a new run ID, which gives the clearest audit trail; or
2. repeat the same command and selection with `--resume`.

Without `--resume`, reusing an existing run ID fails rather than overwriting it.
With `--resume`, the previous attempt's named artifacts are first moved out of
the active run root into `attempts/<UTC timestamp>/`. The active root then
contains only artifacts written by the current attempt. This prevents an old
successful proposal or review queue from being mistaken for output from a later
failed GLM request. Archived attempts remain available for audit; use the active
root's manifest, proposals, and validation report as the current record.

When deterministic validation fails, the CLI still writes the proposal and
review artifacts with their issues and exits `2`. The invalid candidate remains
isolated: runtime messages, Translation Memory, and approval state are not
changed. Correct the proposal in a separate local file and run `validate` again
under a new run ID.

## Security and governance guarantees

- English `enUS` text remains the canonical source.
- The pipeline never edits `frontend/src/lib/i18n/messages.ts`.
- The pipeline never writes to `localization/translation-memory.jsonl`.
- The pipeline never changes glossary status or creates `approved` decisions.
- Search aliases cannot become display labels or entity-resolution values.
- Product-name candidates remain English at runtime until separately approved.
- Protected professional, academic, and technical terms found in UI copy remain
  English even when the surrounding sentence is localized. Corpus knowledge,
  source excerpts, citations, titles, names, and Qdrant content stay outside
  this UI localization workflow.
- The workflow does not change Source Approval, Claim Review, Storage
  Authorization, evidence provenance, confidence, contradiction tracking, or
  sensitivity controls.
- Z.AI GLM is the only optional LLM boundary; there is no external translation
  platform and no second model provider.
- Assembly is fully offline and accepts only completed GLM `propose` runs whose
  manifest-to-proposal integrity and current-catalog identity can be proven.
- Assembly records input lineage and implementation fingerprints but never
  changes candidate status or turns machine output into approved knowledge.
- MagicForge does not employ a human translation service. Any later
  product-owner or domain-owner decision authorizes governance; it does not
  create the translation.

The machine proposal queue is described in
`localization/review/README.md`.
