# Localization review and machine proposal queues

MagicForge separates machine-generated localization candidates from governance
decisions.

- Machine proposal queues are created under
  `localization/runs/<run-id>/review-queue.jsonl`.
- This `localization/review/` directory is reserved for explicit governance
  decision records.

Neither location deploys translations. The implemented CLI has no apply or
publish command and does not modify runtime messages, the glossary, Translation
Memory, or an `approved` state.

MagicForge does not use a human translation service. GLM proposes the wording.
A later product owner or domain owner may authorize or reject governed use, but
that role is governance review rather than human translation.

## Machine proposal queue

Create an offline plan first:

```bash
python -m localization.pipeline propose \
  --module chat \
  --run-id localization-chat-plan-001
```

The command above makes no model call and produces no proposal queue. Candidate
generation requires the explicit Z.AI API boundary:

```bash
python -m localization.pipeline propose \
  --module chat \
  --run-id localization-chat-glm-001 \
  --use-glm
```

`--use-glm` uses MagicForge's existing GLM provider. It is not offline
inference. It does not use an external translation platform or a second LLM.

Each `review-queue.jsonl` record contains:

```json
{
  "proposal": {
    "key": "chat.composer.submit",
    "source_text": "Set the card",
    "source_hash": "<sha256>",
    "candidate_chinese": "放下问题牌",
    "status": "machine_proposed",
    "provider": "glm",
    "generated_by": "local_ai"
  },
  "validation": {
    "valid": true,
    "issues": []
  },
  "runtime_applied": false,
  "translation_memory_written": false,
  "governance_decision": "pending"
}
```

The real proposal object also carries confidence, rationale, source and target
locales, protected terms, and warnings. The queue wraps it with deterministic
per-key findings. `validation.valid: true` means only that automated policy
checks passed; it does not mean approved, published, scientifically verified,
or human reviewed.

An existing proposal file can be checked again without a model call:

```bash
python -m localization.pipeline validate \
  --input localization/runs/localization-chat-glm-001/proposals.jsonl \
  --run-id localization-chat-validation-001
```

The validation run creates its own `review-queue.jsonl` and does not modify the
input file. It validates only the proposal records supplied in that file. Its
manifest records the input SHA-256, `validation_scope:
supplied_proposal_records`, and `proposal_provenance:
supplied_input_unverified`; unknown external-platform or human-origin facts are
recorded as `null`, not guessed as `false`.

## Review lanes

| Lane | Governance owner | Typical content |
| --- | --- | --- |
| Product naming | Product owner / brand owner | Module names, space titles, taglines |
| Magic terminology | Magic domain owner | Misdirection, False Transfer, Technique, Method |
| Academic terminology | Cognitive-science domain owner | Inattentional Blindness, Attention, evidence language |
| Citation safety | Research/citation governance owner | Titles, names, citations, supplemental translations |

These owners decide whether governed wording may be used. They are not asked to
translate the source copy.

## Decision record

Future governance decision files should be named `review-YYYYMMDD-NNN.yaml` and
contain at least:

```yaml
review_id: review-YYYYMMDD-NNN
term_id: product.magic_chat
source_term: Magic Chat
proposed_chinese: 魔术智询
decision: approved | rejected | revise
reviewer: ""
reviewer_role: ""
review_date: ""
reason: ""
affected_scopes: []
glossary_version: "1.2"
```

## Promotion rule

1. GLM output begins as `machine_proposed`; a deterministic pass may validate
   it but cannot approve it.
2. The run queue remains `governance_decision: pending`, with
   `runtime_applied: false` and `translation_memory_written: false`.
3. A later authorized governance owner may record an `approved`, `rejected`, or
   `revise` decision in this directory.
4. Only an explicitly approved term or translation unit may become eligible for
   a separately implemented promotion process.
5. The current pipeline provides no promotion process and never writes runtime
   messages or `translation-memory.jsonl`.

Review approval never changes Source Approval, Claim Review, Storage Authorization, evidence provenance, or corpus verification status.

## Failure isolation

If a Z.AI request fails, its run records `failure.json` and does not produce a
usable machine queue. Fix the GLM configuration or connectivity and rerun with a
new run ID, or repeat the identical command with `--resume`. Validation errors
remain attached to the local queue and block a successful exit, but never alter
runtime or Translation Memory.

On `--resume`, named artifacts from the previous attempt are moved into
`attempts/<UTC timestamp>/` before the new attempt begins. The active run root
therefore cannot expose an older `proposals.jsonl` or `review-queue.jsonl` as if
it belonged to a later failed attempt.
