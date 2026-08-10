# MagicForge Translation Policy v1.2

Status: `active_governance`
Scope: terminology governance and local AI candidate generation
Runtime localization infrastructure authorized: **Yes**
Automatic machine deployment authorized: **No**

This policy is authoritative for terminology display. It supersedes conflicting display recommendations in earlier localization guides or brand-voice examples; those files are outside this revision's four-file boundary.

## 1. Objective

MagicForge is a professional magic and cognitive-science research instrument. Localization does not seek maximum Chinese coverage.

The governing principle is:

> **English canonical term + optional Chinese support**

English is the source of truth. Chinese is permitted only for:

- normal UI accessibility;
- search discovery;
- compact bilingual labels where Chinese materially improves usability.

The intended experience is:

> A Chinese interface for an international professional magic research instrument.

## 2. Separation of responsibilities

The glossary governs names, aliases, display modes, and review status. It is not a teaching document.

Concept definitions and educational explanations belong in:

- Knowledge Nodes;
- Evidence Cards;
- Research Notes.

Glossary `explanation` is optional and may be used only to prevent a terminology-governance ambiguity, such as distinguishing `Method` from research method or one confidence dimension from another.

## 3. Three localization layers

### Layer 1 — Preserve English only

Professional terminology remains English in the interface by default. Chinese must not automatically replace or accompany it.

#### Magic theory and technique

English-only defaults include:

- `Misdirection`
- `False Transfer`
- `Switch`
- `Steal`
- `Load`
- `Effect`
- `Method`
- `Technique`

Chinese aliases may exist for search discovery only.

Examples:

```yaml
english: "Misdirection"
chinese: null
aliases:
  - 注意转移
  - 注意引导
display_mode: english_only
```

```yaml
english: "False Transfer"
chinese: null
aliases:
  - 假转移
  - 假传递
display_mode: english_only
```

#### Cognitive science

English-only defaults include:

- `Inattentional Blindness`
- `Change Blindness`
- `Cognitive Load`
- `Priming`
- `Prediction Error`
- `Cognitive Mechanism`
- `Attention`
- `Selective Attention`

Chinese equivalents belong in search aliases unless a later human decision documents a strong usability reason for display.

#### Technical names

Always preserve exact form:

- `Qdrant`
- `GLM`
- `React`
- `Next.js`
- `GitHub`
- `Vercel`
- `FastAPI`
- `TypeScript`
- `API`
- `JSON`
- `JSONL`
- `YAML`
- `DOI`
- `ISBN`

### Layer 2 — Limited bilingual display

Use only for short labels where Chinese materially improves interface usability without replacing professional identity.

Approved-form examples, still `draft` until human review:

```text
Evidence Card
证据卡

Knowledge Node
知识节点

Provenance
溯源
```

Professional magic exceptions proposed by the revision are:

| English canonical | Compact Chinese support |
| --- | --- |
| Palm | 藏牌手法 |
| Force | 强选 |
| Vanish | 消失 |
| Cold Reading | 冷读 |

These are draft exceptions, not runtime authorization. English remains canonical and visually identifiable. The Chinese line must be a short label, never a definition or teaching sentence.

`Performer` may use `表演者` as a compact role label because it improves entity-role recognition without redefining a professional technique.

Limited bilingual entries must not contain explanatory display copy such as:

```text
Evidence Card
用于承载单一可核验主张、来源、定位、局限和置信度的结构化证据对象
```

That content belongs in knowledge or product documentation.

### Layer 3 — Normal UI translation

Ordinary interface language may use natural Chinese after approval:

| English | Chinese |
| --- | --- |
| Search | 搜索 |
| Settings | 设置 |
| Filter | 筛选 |
| Save | 保存 |
| Delete | 删除 |
| Navigation | 导航 |

Product names and professional terms never enter this layer automatically.

## 4. Display modes

| Mode | Rule |
| --- | --- |
| `english_only` | Display the English canonical term only; Chinese fields and aliases are non-display metadata. |
| `limited_bilingual` | Display English plus an approved compact Chinese label; no explanatory sentence. |
| `localized_ui` | Display an approved natural Chinese UI translation. |
| `product_name_review` | Display exact English until a product-name decision is approved. |
| `source_original` | Preserve original source content; a reviewed supplement may appear separately. |

The presence of `term.chinese` never selects a display mode. `display.mode` and `term.status` control runtime eligibility.

## 5. Search-alias governance

Chinese aliases exist only for discovery.

Aliases must not:

- appear automatically as labels;
- replace the English canonical term;
- enter translation memory;
- change ontology values;
- resolve or merge entities;
- overwrite source or citation content.

Search should return the English canonical term after matching an alias.

## 6. Professional magic term decisions

The governed forms are:

| Term | Chinese field | Search aliases | Display mode |
| --- | --- | --- | --- |
| Misdirection | `null` | 注意转移, 注意引导 | `english_only` |
| False Transfer | `null` | 假转移, 假传递 | `english_only` |
| Palm | 藏牌手法 | 掌藏 | `limited_bilingual` |
| Force | 强选 | 强制选择 | `limited_bilingual` |
| Switch | `null` | 偷换, 替换 | `english_only` |
| Vanish | 消失 | — | `limited_bilingual` |
| Steal | `null` | 秘密取入 | `english_only` |
| Load | `null` | 秘密置入 | `english_only` |
| Cold Reading | 冷读 | — | `limited_bilingual` |

Do not use long Chinese constructions such as `注意引导机制`, `假转移机制`, `手中隐蔽持物技法`, `强制选择机制`, `消失效果`, or `冷读信息推断法` as glossary labels.

## 7. Product names

Keep exact English names until human approval:

- `Magic Chat`
- `Evidence Browser`
- `Knowledge Explorer`
- `Corpus Dashboard`
- `Research Console`

Chinese candidates may remain in the glossary as review proposals. They cannot become aliases, translation-memory units, locale values, or runtime labels while status is `draft` or `reviewed`.

A module name, physical-space title, and tagline require separate keys.

## 8. Glossary schema

Every entry contains:

```yaml
- term_id: magic.misdirection
  term:
    english: "Misdirection"
    chinese: null
    aliases:
      - 注意转移
      - 注意引导
    category: magic_theory
    status: draft
  display:
    mode: english_only
```

Required `term` fields are:

- `english`
- `chinese`
- `aliases`
- `category`
- `status`

`explanation` is optional. It must be short and only resolve governance ambiguity. Definitions, mechanisms, applications, examples, and limitations do not belong in the glossary.

## 9. Status governance

Allowed values:

- `draft`: proposed and not approved;
- `reviewed`: domain review completed, but not authorized for runtime;
- `approved`: explicitly approved for its documented mode and scope;
- `rejected`: unavailable for use, retained for decision history.

Every new or revised entry starts as `draft`.

Only a human decision record may set `approved`. An LLM, script, extractor, migration, or translation tool may never promote status automatically.

Approval is scoped. Approving a search alias does not approve bilingual display; approving a UI label does not approve a canonical-term replacement.

## 10. Protected source and knowledge content

Never automatically translate or overwrite:

- `MagicForge`;
- author and performer names;
- paper, book, and source titles;
- citations and direct quotations;
- Evidence Card claims, excerpts, and limitations;
- DOI, ISBN, URL, ORCID, IDs, schema versions, enum values, API paths, collection names, and run IDs.

Any future reviewed Chinese supplement must remain separate from the original and cannot participate in entity resolution.

## 11. Translation memory

Translation memory contains only human-approved runtime units.

- Search aliases never enter translation memory.
- Product candidates never enter translation memory.
- `english_only` entries have no Chinese runtime unit.
- A `limited_bilingual` unit stores the English canonical term and compact Chinese label as distinct values.
- Current translation-unit count must remain zero.

## 12. Local AI pipeline boundary

MagicForge now has runtime `en-US` / `zh-CN` interface infrastructure. The local
pipeline may:

- inventory the English canonical catalog and its UI usage context;
- classify strings with this policy and `do-not-translate.yaml`;
- ask the existing GLM adapter for structured Chinese UI candidates;
- run deterministic terminology, placeholder, and provenance checks;
- write local, reproducible proposal and validation artifacts.

The pipeline is local-first: inventory, classification, validation,
and artifact creation run locally. A GLM request happens only after an explicit
CLI flag. MagicForge does not use an external translation platform or a second
LLM provider.

The pipeline must not:

- alter the English canonical catalog;
- send corpus content, claims, excerpts, citations, titles, names, or identifiers
  to localization generation;
- write a machine proposal directly into runtime messages;
- mark a machine proposal or glossary term `approved`;
- write draft proposals into `translation-memory.jsonl`;
- turn search aliases into display labels or entity-resolution values;
- translate protected knowledge content.

Local AI produces the wording. A later product-owner or domain-owner decision is
a governance authorization, not a human translation service.
