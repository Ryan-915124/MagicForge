# MagicForge Chinese Brand Voice v1.0

Status: `draft_for_human_review`
Language: `zh-CN` with English professional terminology
Runtime use authorized: **No**

## 1. Identity

MagicForge should feel like:

> 一件会说中文的国际专业魔术研究仪器。

It should not feel like:

> 一个把所有英文都翻成中文的应用。

The voice combines professional magic culture, cognitive-science precision, evidence discipline, performance craft, and restrained mystery.

## 2. What English and Chinese each contribute

English provides:

- professional identity;
- internationally recognized magic vocabulary;
- academic precision;
- compatibility with papers, books, lectures, and practitioner discourse;
- stable canonical terms across languages.

Chinese provides:

- explanation;
- accessibility;
- learning support;
- natural UI actions and guidance;
- clear evidence boundaries for Chinese readers.

Chinese must not replace English merely to increase translation coverage.

## 3. Voice qualities

MagicForge speaks with:

- **precision** — it names the exact object, action, and evidence state;
- **restraint** — it does not exaggerate evidence or theatrical language;
- **craftsmanship** — it uses concrete language drawn from instruments, archives, worktables, stages, and research practice;
- **mystery** — it creates discovery and reveal without fantasy-game language;
- **epistemic honesty** — it distinguishes research, practitioner knowledge, and interpretation.

The tone is intelligent, calm, exact, and slightly mysterious. It is never childish, mystical, sales-driven, or falsely authoritative.

## 4. Three language layers

### Layer 1 — Preserve the English canonical term

Professional magic terms, selected cognitive-science concepts, technical names, people, titles, citations, and identifiers retain English or original-language canonical values.

Do not write:

```text
误导
```

as a replacement for `Misdirection`.

Do write:

```text
Misdirection
注意引导机制
```

The Chinese line explains the concept; it does not rename it.

### Layer 2 — English-primary bilingual explanation

Use bilingual display when Chinese helps comprehension and English preserves professional recognition.

Preferred detailed layout:

```text
Inattentional Blindness
非注意盲视
```

Preferred first inline mention:

```text
Inattentional Blindness（非注意盲视）
```

For later mentions in the same context, keep the English term unless an approved component-specific pattern says otherwise. Never silently drop English and continue with an unapproved Chinese substitute.

### Layer 3 — Natural Chinese UI

Ordinary actions and guidance should use direct modern Chinese:

- `Search` → `搜索`
- `Filter` → `筛选`
- `Settings` → `设置`
- `Save` → `保存`
- `Delete` → `删除`

Product module names do not automatically belong to Layer 3. Until human approval, display their English names.

## 5. Professional terminology voice

### Magic terms

Magic means performance magic, not supernatural magic. Use `魔术` in Chinese explanation and never `魔法`.

The professional term stays English:

- `Misdirection` — `注意引导机制`
- `False Transfer` — `假转移机制`
- `Palm` — explanatory Chinese may describe concealed holding;
- `Force`, `Switch`, `Vanish`, `Steal`, `Load`, `Cold Reading` — retain English identity and use reviewed explanations only.

Chinese should describe function rather than force a literal dictionary equivalent.

### Cognitive-science terms

Keep the English scientific construct visible:

- `Inattentional Blindness` — `非注意盲视`
- `Change Blindness` — `变化盲视`
- `Cognitive Load` — `认知负荷`
- `Priming` — `启动效应`
- `Prediction Error` — `预测误差`

The wording must follow the cited study's construct. A Chinese explanation never expands a narrow result into a universal principle.

### Evidence concepts

Use English-primary bilingual display for named MagicForge structures:

- `Evidence Card` — `证据卡`
- `Knowledge Node` — `知识节点`
- `Cognitive Mechanism` — `认知机制`

Model objects and world-building objects use different keys. `Evidence Card` is a data model; an archive-themed visual object may be an `Evidence Dossier`. `Knowledge Node` is a graph-compatible object; a visual scene may call it a concept or artifact. The metaphor cannot overwrite the model term.

## 6. Product naming voice

Product names must express creation, exploration, discovery, revelation, and research. Avoid names that sound like:

- AI customer service;
- consulting chatbots;
- generic enterprise software;
- ordinary file databases;
- fantasy games.

The current Chinese names are candidates only. In particular:

- `魔术智询` is withdrawn as a recommendation because it sounds like AI consultation;
- `语料观测台` is withdrawn as a recommendation because it sounds too NLP-specific;
- `证据档案馆`, `知识星图`, and `研究实验台` remain candidates, not approved names.

Until review is complete, use `Magic Chat`, `Evidence Browser`, `Knowledge Explorer`, `Corpus Dashboard`, and `Research Console`.

A module name, physical-space title, and tagline always use separate keys.

## 7. Evidence language

### Preserve epistemic force

Chinese must retain causality, uncertainty, population, task, and limitation boundaries.

- If a source says `may`, use `可能` or `或许`, not `必然`.
- If a result is correlational, do not write `导致`.
- If evidence is limited to one experiment, do not write `研究已经证明`.
- If a claim is unverified, do not write it as established knowledge.

Preferred patterns:

- `研究提示，在该实验条件下……`
- `这一结果适用于该样本与任务设置。`
- `现有证据尚不足以确定……`
- `该来源报告……`

Use `证明` only when the source, evidence class, and context genuinely support that strength.

### Separate knowledge origins

| Layer | Recommended Chinese framing | Never imply |
| --- | --- | --- |
| Scientific Evidence | `研究结果提示……` | That expert authority equals experiment |
| Expert Practice | `该魔术实践来源建议……` | That practice advice is scientific evidence |
| MagicForge Interpretation | `MagicForge 的解读是……` | That analysis is a source quotation |

`Confidence` is an estimate about a named object. Never display it as a generic truth score or “90% correct.” Keep `Extraction Confidence` and `Evidence Confidence` distinct.

## 8. UI writing

### Actions

Use concrete verbs: `搜索`, `筛选`, `展开`, `定位`, `比较`, `核验`, `保存`. Avoid vague AI-product language such as `赋能`, `开启无限可能`, or `智能升级体验`.

### Status

Describe only what the system actually knows:

- `已配置` does not mean `连接正常`;
- `已生成` does not mean `经人工核验`;
- `Source Approval` does not imply `Claim Review`;
- `已投影` does not mean `已进入生产知识库`.

Bootstrap content must remain visibly unverified.

### Errors

State what happened and what the user can do. Technical identifiers remain unchanged; secrets and local paths remain hidden.

Preferred:

```text
回答请求超时。请重试；如果问题持续出现，请检查 API 与 GLM 连接。
```

Avoid:

```text
星图失去了魔力。
```

### Empty states

Explain what is absent and what action can make it appear. Do not create the impression that research or verification has already occurred.

## 9. Source and citation voice

Author names, performer names, paper and book titles, quotations, and complete citations stay in their original form.

Translate only surrounding UI labels. A reviewed Chinese alias or supplemental title may be displayed next to the original, never instead of it, and must carry an explicit review state.

Claims, excerpts, and limitations are knowledge content, not UI copy. They require a separate localization workflow with provenance and human review.

## 10. Chinese typography

- Use natural modern Chinese and full-width Chinese punctuation in Chinese prose.
- Keep necessary spacing between Chinese and English terms or numbers: `使用 GLM 提取 12 条 Claim`.
- Preserve exact casing and punctuation in names such as `Next.js`, `MagicForge`, and `Qdrant`.
- Use language metadata in a future implementation: English terms should be identifiable as `lang="en"`; Chinese explanations as `lang="zh-CN"`.
- Do not use decorative spacing, pseudo-classical Chinese, or all-caps English merely to appear premium.

## 11. Forbidden voice patterns

Avoid:

- forced Chinese replacements that hide international professional terms;
- `魔法`, `召唤答案`, `神谕`, or fantasy-game ranks;
- generic AI claims such as `重新定义可能`;
- unsupported authority such as `绝对正确` or `真相就是`;
- theatrical error messages that conceal system state;
- using one Chinese word to collapse Source, Provenance, Citation, and Knowledge Origin.

## 12. Review checklist

Before approval, ask:

1. Is this ordinary UI, a product name, a professional magic term, a scientific construct, a source value, or knowledge content?
2. Does English need to remain canonical and visible?
3. Is the Chinese text a replacement, an explanation, or a reviewed supplemental alias?
4. Does the Chinese alter causality, confidence, limitation, or verification status?
5. Does it preserve the difference between scientific evidence, expert practice, and MagicForge interpretation?
6. Has the correct human domain reviewer approved it?

No draft wording may enter runtime localization or translation memory.
