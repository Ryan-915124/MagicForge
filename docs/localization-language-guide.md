# MagicForge Localization Language Guide v0.1

Status: **Draft for human review**
Locales: `en-US` → `zh-CN`
Runtime impact: **None**

This guide establishes how MagicForge should speak in Chinese before any i18n framework, locale file, string extraction, or language switcher is introduced. Every proposed Chinese product name remains unapproved.

## 1. Deliverable boundary

This foundation adds governance assets only:

- `localization/glossary.en-zh.yaml`: controlled terminology proposals;
- `localization/brand-voice.md`: Chinese product voice and evidence-language rules;
- `localization/do-not-translate.yaml`: protected names, content, citations, and identifiers;
- `localization/translation-memory.jsonl`: an empty, versioned translation-memory container;
- `localization/review/`: the future human decision queue.

It does **not**:

- add an i18n dependency;
- extract or replace frontend strings;
- create locale bundles;
- translate knowledge claims, source excerpts, papers, or citations;
- change the current English UI;
- approve any candidate terminology.

## 2. Chinese language identity

MagicForge should sound like a precise private instrument built for magicians: calm, intelligent, crafted, and slightly mysterious. It should not sound like a generic AI product, a literal enterprise dashboard, or a fantasy game.

The language model is:

> Modern research Chinese, shaped by magic craftsmanship and restrained theatrical rhythm.

Atmosphere belongs in room titles, transitions, and discovery moments. Accuracy takes priority in actions, errors, system states, evidence claims, and governance labels.

Detailed copy rules and examples live in [`localization/brand-voice.md`](../localization/brand-voice.md).

## 3. Source-of-truth order

When rules conflict, apply them in this order:

1. **Do-not-translate rules** — protect originals and identifiers.
2. **Approved glossary entries** — define product names and controlled terms.
3. **Brand voice** — determines tone, sentence form, and module-specific vocabulary.
4. **Approved translation memory** — reuses reviewed translations in matching context.
5. **New proposal** — anything unresolved goes to human review; it is never silently promoted.

An entry's `recommended_chinese` is only a recommendation. It becomes usable as a canonical translation only when `status: approved` and a corresponding decision exists under `localization/review/`.

## 4. Five translation lanes

The localization system must classify text before translating it.

| Lane | What belongs here | Treatment |
| --- | --- | --- |
| UI translation | Buttons, labels, help text, empty states, errors | Translate using approved terms and voice rules |
| Product naming | Module names, named instruments, branded spaces | Human naming review; dedicated keys |
| Magic terminology | Ontology terms and practitioner vocabulary | Practitioner review; preserve domain meaning |
| Academic terminology | Scientific constructs and evidence language | Academic review; preserve uncertainty and source usage |
| Source citation | Authors, performers, titles, citations, locators | Translate surrounding labels only; preserve content |

Knowledge content is a sixth, separately governed content-localization workflow. Evidence claims, excerpts, and limitations must never be swept into UI string extraction.

## 5. Proposed product names

These are review proposals, not approved translations.

| term_id | English | Candidates | Recommendation | Reason | Status |
| --- | --- | --- | --- | --- | --- |
| `product.magic_chat` | Magic Chat | 魔术智询 / 魔术对话 / 魔术工作台 | **魔术智询** | Signals magician-specific inquiry and analysis without becoming generic chat | Pending review |
| `product.evidence_browser` | Evidence Browser | 证据档案馆 / 证据检索 / 证据浏览器 | **证据档案馆** | Expresses preservation, inspection, and provenance rather than a software browser | Pending review |
| `product.knowledge_explorer` | Knowledge Explorer | 知识星图 / 魔术知识星图 / 知识探索器 | **知识星图** | Matches relationship discovery without falsely implying a graph database | Pending review |
| `product.corpus_dashboard` | Corpus Dashboard | 语料观测台 / 知识库总览 / 语料库仪表盘 | **语料观测台** | Preserves the research-corpus meaning and observatory metaphor | Pending review |
| `product.research_console` | Research Console | 研究实验台 / 研究控制台 / 研究工作台 | **研究实验台** | Combines technical operation with the research-laboratory space | Pending review |

### Product name versus room title

A module name, a physical-space title, and a tagline require separate localization keys. For example:

```text
product.magic_chat.name              Magic Chat
experience.magic_chat.space_title    The private worktable
experience.magic_chat.tagline        ...
```

The product name must not be overwritten by a more decorative space title. This prevents all five modules from losing their navigation identity when their visual metaphors evolve.

## 6. Core terminology decisions awaiting review

### Magic ontology

| English | Proposed Chinese | Required distinction |
| --- | --- | --- |
| Effect | 魔术效果（compact UI: 效果） | Audience-perceived event; never secret implementation or scientific effect |
| Method | 秘密方法 | Secret causal implementation; research method uses a different key |
| Technique | 技法 | Executable, trainable performance skill |
| Misdirection | 误导 | Umbrella principle; use 误导技法 only for executable application |
| False Transfer | 假传递（False Transfer） | Practitioner review required before dropping the English on first use |
| Performer | 表演者 | Entity label is translated; a performer's name is not |

The key semantic boundary is:

```text
Effect     = what the audience experiences
Method     = how it is secretly achieved
Technique  = what a performer can execute and train
```

Translation must not collapse these three concepts.

### Scientific vocabulary

`Attention` is proposed as **注意** in academic contexts and may become **注意力** only in general-user prose. `Cognitive Mechanism` is **认知机制** and must remain a scientific explanatory construct, not a brain region, experimental condition, or applied magic principle.

`Inattentional Blindness` currently has the recommendation **无意视盲**, with **非注意盲视** retained as a search alias. Chinese literature uses more than one rendering, so this entry is explicitly marked `needs_academic_review`; one official Chinese term must be selected for UI display while aliases remain available only to search.

### Evidence vocabulary

The following concepts must remain separate:

| Concept | Proposed Chinese | Not equivalent to |
| --- | --- | --- |
| Source | 来源 | Provenance or citation |
| Provenance | 溯源信息 | A single bibliographic record |
| Citation | 引用信息 | A translated citation body |
| Locator | 可核验定位 | A vague source URL without location |
| Knowledge Origin | 知识来源类别 | A specific Source entity |
| Extraction Confidence | 抽取置信度 | Evidence support |
| Evidence Confidence | 证据支持置信度 | Truth probability |

`Review` also requires contextual keys:

- evidence class `review` → **综述**;
- workflow action `review` → **审核**;
- `peer review` → **同行评审**.

## 7. Evidence-language policy

Localization must preserve epistemic force.

### Scientific evidence

- Preserve population, task, condition, and limitation language.
- Do not change “associated with” to “causes.”
- Do not change “may” to “will.”
- Do not turn a study result into a universal magic rule.

### Expert practice

Translate as expert or magic-practice knowledge. A respected practitioner is still not a controlled experiment. The Chinese wording must not promote authority into scientific evidence.

### Interpretation

Label MagicForge's analysis explicitly. Use wording such as `MagicForge 解读` or `可以据此推测`, and never make it appear inside a source quotation.

## 8. Protected content

The following are never automatically translated:

- `MagicForge`;
- author names;
- performer names;
- paper and book titles;
- complete citations;
- DOI, ISBN, URL, ORCID, IDs, schema versions, run IDs, collection names, enum values, JSON keys, and API paths;
- Evidence Card claims, source excerpts, and limitations in the normal UI-copy workflow.

An established Chinese person name or title translation can be added only as a reviewed supplemental alias. The original remains canonical and visible. Translation aliases never resolve duplicate entities.

The machine-readable policy is in [`localization/do-not-translate.yaml`](../localization/do-not-translate.yaml).

## 9. Translation memory policy

`translation-memory.jsonl` intentionally contains only one metadata record and zero translation units.

A future approved translation unit should follow this shape:

```json
{"record_type":"translation_unit","unit_id":"ui.example.action","source_locale":"en-US","target_locale":"zh-CN","source_text":"Open dossier","target_text":"展开卷宗","scope":"ui_translation","context":"Evidence Browser dossier action","glossary_refs":["experience.evidence.dossier"],"status":"approved","reviewer":"...","review_date":"YYYY-MM-DD","source_hash":"..."}
```

Rules:

1. Only human-approved units enter translation memory.
2. A unit must carry context and scope; source-text matching alone is insufficient.
3. Changed source text creates a new review requirement rather than silently reusing stale Chinese.
4. Knowledge-content translations require source ID, locator, original text, and a separate content-review status.
5. Rejected or superseded wording remains in review history, not in active memory.

## 10. Human review workflow

```text
Term or copy proposal
        ↓
Scope classification
        ↓
Do-not-translate check
        ↓
Domain review (product / magic / academic / citation)
        ↓
Decision record in localization/review/
        ↓
Glossary status becomes approved
        ↓
Approved translation unit may enter translation memory
        ↓
Later i18n implementation
```

Minimum reviewers:

- product owner or brand reviewer for the five module names;
- practicing magician/domain editor for magic vocabulary;
- Chinese-language psychology/research editor for scientific and evidence vocabulary;
- citation/research reviewer for supplemental title or name aliases.

Localization review cannot approve a source, claim, Knowledge Node, or storage action. Existing MagicForge governance gates remain independent.

## 11. Review decisions requested

Before implementation, reviewers should decide:

1. Approve, revise, or reject each of the five proposed product names.
2. Select the canonical Chinese display term for `Inattentional Blindness`; keep alternatives as search aliases only.
3. Confirm `假传递（False Transfer）`, `误导`, `技法`, and `秘密方法` with a Chinese-speaking magician.
4. Confirm whether `Confidence` should display as `置信度` in all research-facing contexts, while retaining separate extraction and evidence keys.
5. Confirm the difference between model objects (`证据卡`, `知识节点`) and visual-world objects (`证据卷宗`, `知识器物`).

Only after these decisions should MagicForge select an i18n implementation skill or library and begin hardcoded-string migration.
