# MagicForge Qdrant Metadata Schema v0.1

## 1. 设计目标

Qdrant 是 MagicForge v0.1 的**检索投影**，不是论文仓库、审核系统或未来知识图谱的替代品。Qdrant 中的一个 point 应表示一个已经人工批准的可检索知识单元：

- 一张原子 Evidence Card 的检索投影；或
- 一个由已批准 Evidence Cards 支持的领域 Knowledge Node 投影。

禁止以下路径：

```text
PDF -> chunk -> embedding -> Qdrant       # 禁止
search snippet -> embedding -> Qdrant     # 禁止
source approval -> automatic ingestion    # 禁止
GLM output -> automatic ingestion         # 禁止
```

目标路径是：

```text
Source -> human source approval -> located claim extraction
       -> Evidence Card -> human claim approval
       -> Knowledge Node / Evidence projection -> embedding -> Qdrant
```

原始正文、PDF 分块和最小证据片段保留在受审核/权限控制的来源与 Evidence 存储中。默认 embedding 文本只由已批准的结构化字段组成，不直接嵌入原始论文段落。

## 2. Point 类型与粒度

所有 point 使用 UUID，payload 的 `knowledge_unit_id` 与 point ID 相同。

### 2.1 Evidence point

一条已批准、可定位的原子主张。它保留 `evidence_card_id`、`canonical_claim_id`、来源和限制条件，适合回答“有何证据”“证据强度如何”。一个来源中的多个主张生成多个 point，不按页或固定 token 数生成 point。

### 2.2 Knowledge node point

一个 Effect、Method、Technique、Psychology/Mechanism 或 Performance 概念的已批准说明。它可以由多张 Evidence Card 支持，通过 `supporting_evidence_ids` 追溯；没有证据卡的个人解释必须明确标为 `personal_interpretation`，不能伪装为科学结论。

`Source` 与 `ResearchPaper` 仍是 provenance/图兼容实体，不作为“论文内容 point”直接参与普通问答检索。

## 3. 目标 payload

示意结构如下。数组字段使用 Qdrant keyword 数组语义，便于精确过滤：

```json
{
  "schema_version": "qdrant-0.1",
  "knowledge_unit_id": "<uuid>",
  "knowledge_type": "evidence",
  "text": "<approved structured retrieval text>",
  "title": "<human-readable unit title>",

  "domain": ["theory", "close-up"],
  "ontology_paths": ["psychology.attention.inattentional_blindness"],
  "topic_tags": ["misdirection", "secret-action-detection"],

  "knowledge_origin": "scientific_evidence",
  "evidence_level": "empirical",
  "evidence_class": "controlled_experiment",
  "confidence": 0.72,
  "confidence_label": "moderate",
  "limitations": ["<approved limitation>"],

  "source_type": "journal_article",
  "source_id": "<uuid>",
  "citation_id": "<uuid>",
  "source_candidate_id": "<uuid>",
  "document_id": "<uuid>",
  "source_locator": "page 7, Results, paragraph 3",
  "page_number": 7,
  "source_year": 2020,

  "evidence_card_id": "<uuid>",
  "canonical_claim_id": "<uuid>",
  "supporting_evidence_ids": ["<uuid>"],
  "contradicting_evidence_ids": [],
  "contradiction_status": "none_found",

  "entity_ids": ["<uuid>"],
  "entity_types": ["cognitive_mechanism", "psychology_principle"],
  "relationship_ids": ["<uuid>"],
  "relation_types": ["explains", "related_to"],

  "secret_exposure_level": "general_principle",
  "secret_exposure_rank": 1,
  "sensitive_information_level": "controlled",
  "claim_eligibility": true,
  "storage_permission": true,
  "approved": true,
  "review_status": "ingested",
  "review_item_id": "<uuid>",
  "reviewed_at": "<RFC3339 datetime>",
  "content_version": 1
}
```

这只是目标契约，不表示当前 Qdrant collection 已迁移。

## 4. 字段字典

### 4.1 核心与路由字段

| 字段 | payload 类型 | 必填 | 索引 | 约束/用途 |
|---|---|---:|---|---|
| `schema_version` | keyword | 是 | 是 | 固定 `qdrant-0.1`；避免跨版本误读 |
| `knowledge_unit_id` | keyword/UUID | 是 | 是 | 必须等于 point ID，稳定且不可复用 |
| `knowledge_type` | keyword | 是 | 是 | `effect`, `method`, `technique`, `psychology`, `evidence`, `performance` |
| `text` | string | 是 | 否 | 唯一用于 embedding 的已批准结构化文本，不是原始 PDF chunk |
| `title` | string | 是 | 否 | 人类可读标题 |
| `domain` | keyword[] | 是 | 是 | `card`, `close-up`, `stage`, `mentalism`, `theory`；至少一个 |
| `ontology_paths` | keyword[] | 是 | 是 | 层级路径，如 `misdirection.temporal`；至少一个 |
| `topic_tags` | keyword[] | 否 | 是 | 辅助路由；不能替代 canonical entity ID |
| `knowledge_origin` | keyword | 是 | 是 | `scientific_evidence`, `expert_practice`, `personal_interpretation` |

`knowledge_type` 是面向检索的投影类别，不替代 `EntityType`。例如 `CognitiveMechanism` 和 `PsychologyPrinciple` 都投影为 `knowledge_type=psychology`，原始实体类型仍保存在 `entity_types`。Expertise/Training 内容在 v0.1 投影为 `performance`，并通过 `ontology_paths=expertise.*` 或 `training.*` 精确路由，避免未经版本升级擅自扩大枚举。

### 4.2 证据字段

| 字段 | payload 类型 | 必填 | 索引 | 约束/用途 |
|---|---|---:|---|---|
| `evidence_level` | keyword | 是 | 是 | `empirical`, `review`, `practitioner`, `anecdotal` |
| `evidence_class` | keyword | 是 | 是 | Evidence Schema 中的细粒度类别 |
| `confidence` | float | 是 | 是 | `0..1`，证据审核分；不是 GLM extraction confidence |
| `confidence_label` | keyword | 是 | 是 | `insufficient`, `low`, `moderate`, `high` |
| `limitations` | string[] | 是 | 否 | 回答时必须随主张返回 |
| `evidence_card_id` | keyword/UUID | Evidence point 必填 | 是 | 指向 Evidence Card source of truth |
| `canonical_claim_id` | keyword/UUID | Evidence point 必填 | 是 | 聚合同义主张 |
| `supporting_evidence_ids` | keyword[] | Knowledge node 必填 | 是 | 支持当前综合/节点的卡片；可为空但只能是解释类内容 |
| `contradicting_evidence_ids` | keyword[] | 是 | 是 | 已知反证/限定卡片 ID；允许空数组 |
| `contradiction_status` | keyword | 是 | 是 | `not_checked`, `none_found`, `resolved`, `unresolved` |

约束映射：

- `empirical`、`review` 只能与 `knowledge_origin=scientific_evidence` 组合。
- `practitioner` 只能与 `knowledge_origin=expert_practice` 组合。
- `anecdotal` 可用于实践报告或个人解释，但回答必须显示其通道。
- `confidence_label=insufficient` 的 point 不得进入生产 Qdrant。
- Practitioner 内容可以支持实践建议、归属或表演分析，不能因高相似度被当作科学机制证据。

### 4.3 来源与定位字段

| 字段 | payload 类型 | 必填 | 索引 | 约束/用途 |
|---|---|---:|---|---|
| `source_type` | keyword | 是 | 是 | 见下方枚举 |
| `source_id` | keyword/UUID | 是 | 是 | 对应 canonical `Source` entity |
| `citation_id` | keyword/UUID | 是 | 是 | 对应 `CitationRecord` |
| `source_candidate_id` | keyword/UUID | 是 | 是 | 对应 `ResearchCandidate`，维持发现链路 |
| `document_id` | keyword/UUID | 是 | 是 | 对应现有 `KnowledgeMetadata.document_id` |
| `source_locator` | string | 是 | 否 | 人类可复核定位，不得只写标题 |
| `page_number` | integer | 否 | 是 | PDF/书籍适用；与现有索引兼容 |
| `source_year` | integer | 否 | 是 | 时间过滤，不代替 citation 元数据 |

`source_type` v0.1 枚举：

- `journal_article`
- `conference_paper`
- `preprint`
- `academic_book`
- `book_chapter`
- `practitioner_book`
- `web_article`
- `interview`
- `transcript`
- `archival_material`
- `internal_analysis`

来源类型与证据强度相互独立：`journal_article` 不自动等于 `empirical` 或高置信度；`practitioner_book` 不自动成为科学证据。

### 4.4 图兼容字段

| 字段 | payload 类型 | 必填 | 索引 | 约束/用途 |
|---|---|---:|---|---|
| `entity_ids` | keyword[] | 是 | 是 | 当前知识单元涉及的稳定实体 UUID |
| `entity_types` | keyword[] | 是 | 是 | 使用现有八类 `EntityType` |
| `relationship_ids` | keyword[] | 是 | 是 | 稳定 `KnowledgeRelationship.id`；允许空数组 |
| `relation_types` | keyword[] | 是 | 是 | `uses`, `inspired_by`, `requires`, `explains`, `performed_by`, `related_to` |

`Effect / Technique / Method / PsychologyPrinciple / Performer / Source / CognitiveMechanism / ResearchPaper` 继续使用 `knowledge.models` 的稳定 ID。Qdrant payload 是这些 canonical 对象的反向索引，未来图数据库应消费同一 ID 与关系，而不是从向量文本重新抽取实体。

### 4.5 审核与安全字段

| 字段 | payload 类型 | 必填 | 索引 | 约束/用途 |
|---|---|---:|---|---|
| `secret_exposure_level` | keyword | 是 | 是 | `none`, `general_principle`, `method_detail`, `operational_secret` |
| `secret_exposure_rank` | integer | 是 | 是 | 分别为 `0, 1, 2, 3`，供范围过滤 |
| `sensitive_information_level` | keyword | 是 | 是 | `public`, `controlled`, `secret_method`, `restricted`；来自人工审核的治理等级 |
| `claim_eligibility` | bool | 是 | 是 | 必须为 `true`；由审核值 `eligible` / `eligible_with_limits` 派生 |
| `storage_permission` | bool | 是 | 是 | 必须为 `true`；由适用于当前投影的 `derived_knowledge_only` / `derived_with_short_excerpt` 派生 |
| `approved` | bool | 是 | 是 | 必须为 `true`，且必须来自命名人工审核员 |
| `review_status` | keyword | 是 | 是 | 与现有状态兼容：`pending`, `approved`, `rejected`, `ingested`；生产 point 必须为 `ingested` |
| `review_item_id` | keyword/UUID | 是 | 是 | 指向不可变审核审计记录 |
| `reviewed_at` | datetime/string | 是 | 否 | RFC 3339；用于展示与审计 |
| `content_version` | integer | 是 | 是 | 从 1 开始；内容改变必须新版本并重新审核 |

Qdrant 中即使意外出现 `pending` 或 `rejected` point，也必须被检索层硬过滤。`approved` 不是客户端可覆盖的普通查询参数。审核系统保留上述权限枚举作为事实来源；Qdrant 中的两个布尔字段只是对当前投影是否合格的 fail-closed 派生值，不能反向覆盖审核记录。

`sensitive_information_level` 与 `secret_exposure_level` 不同：前者表达治理/访问分类，后者表达文本实际泄露秘密的粒度。服务端必须同时满足治理许可与暴露等级限制。

## 5. Secret exposure policy

| level | rank | 内容 | 默认策略 |
|---|---:|---|---|
| `none` | 0 | 历史、公开表演分析、非秘密描述 | 可进入普通已授权知识检索 |
| `general_principle` | 1 | 注意、记忆、叙事等一般原理，不给出可复制秘密步骤 | 默认最高可见级别 |
| `method_detail` | 2 | 明确揭示方法、机关或关键秘密动作 | 需要应用层授予相应访问级别和明确用途 |
| `operational_secret` | 3 | 可复现的完整步骤、制作细节、专有/高度敏感处理 | 默认拒绝；仅在明确授权和审计场景中检索 |

规则：

1. 每个 point 必须取其所有字段中最高的暴露级别，不能只按标题分类。
2. 检索请求必须在 Qdrant 服务端同时加入 `secret_exposure_rank <= requester_clearance_rank` 和允许的 `sensitive_information_level` 集合；不得先检索后在 GLM 提示词中隐藏。
3. 未提供访问上下文时使用 rank 1；客户端不能自行声明更高 clearance。
4. 高敏感 point 的 payload、日志、缓存和回答引用均遵循同一权限。
5. 科学证据等级与秘密敏感度无关；高质量论文也不能绕过秘密访问控制。
6. 更改敏感级别需要人工复核和新版本，不能由问答 LLM 动态降级。

## 6. 必填校验与 ingestion gate

写入前按以下顺序执行，任一失败都不得 upsert：

1. `CitationRecord` 与 `ResearchCandidate` ID 链一致，来源达到所需核验状态。
2. Evidence Card 具有原子 claim、最小证据片段和可复现 locator。
3. `claim_eligibility=true`，且 claim-level reviewer 已命名并给出理由。
4. `contradiction_status != not_checked` 且 `contradicting_evidence_checked=true` 存在于审核审计中。
5. `storage_permission=true`，敏感级别和版权条件允许目标投影。
6. `approved=true`；仅有 source approval 不满足此条件。
7. 构造知识单元和 embedding 文本，校验枚举、UUID、graph endpoints 和版本。
8. 显式 ingestion 操作先以 `review_status=approved` 写入暂存 point；该状态不满足生产检索硬过滤。
9. 按 manifest 核对完整 point ID 集合、payload checksum 和暂存状态。
10. 只有完整核对成功后，才把整个 manifest 的 point 提升为 `review_status=ingested`，再次核对并签发 Ingestion Receipt。

若写入、状态提升与审核状态更新无法原子完成，检索器仍只接受 `review_status=ingested`，并由同一 deterministic manifest 的重试/reconciliation 处理短暂的不一致。部分暂存写入不会对用户可见，也不会签发 receipt。禁止为了便利放宽为“只要 point 存在即可检索”。

## 7. Payload indexes

目标 collection 至少建立以下索引：

```text
KEYWORD:
  schema_version, knowledge_unit_id, artifact_type, artifact_id,
  knowledge_type, domain,
  ontology_paths, topic_tags, knowledge_origin,
  evidence_level, evidence_class, confidence_label,
  source_type, source_id, citation_id, source_candidate_id, document_id,
  evidence_card_id, canonical_claim_id,
  supporting_evidence_ids, contradicting_evidence_ids, contradiction_status,
  entity_ids, entity_types, relationship_ids, relation_types,
  secret_exposure_level, sensitive_information_level, review_status, review_item_id,
  claim_review_item_ids, storage_manifest_id, manifest_hash

BOOL:
  claim_eligibility, storage_permission, approved

FLOAT:
  confidence

INTEGER:
  page_number, source_year, secret_exposure_rank, artifact_version, content_version
```

不为 `text`、`limitations`、`source_locator` 建 keyword 索引；它们随命中结果返回。需要定位过滤时优先使用 `page_number`、`source_id` 和 `citation_id`。

实现状态：`QdrantService._PAYLOAD_INDEXES` 已迁移到本节的审核、安全、证据、路由、图兼容、manifest 和版本索引。默认 collection 使用 `magicforge_knowledge_v01`，旧 document-chunk collection 不会被原位补标或自动迁移。

## 8. Embedding 文本

embedding 输入由结构化字段按固定顺序生成，并与 payload 一起版本化：

```text
Knowledge type: psychology
Title: Inattentional blindness in performance viewing
Claim/definition: <approved statement>
Mechanism: <approved mechanism>
Magic application: <approved application>
Conditions: <approved context>
Limitations: <approved limitations>
```

规则：

- 不嵌入标题页、参考文献列表、整页 PDF 或任意 token chunk。
- 不把 `evidence_excerpt` 当 embedding 正文；其默认位置是 Evidence/Review 存储。
- Knowledge node 的综合文本必须列出 `supporting_evidence_ids`，且综合内容本身经过审核。
- Practitioner 与 personal interpretation 的通道标签进入 embedding 文本和 payload，避免回答时去除来源性质。
- 更新主张、限制、应用或敏感级别时产生新 `content_version` 并重新 embedding。

## 9. 检索安全基线

所有用户可见搜索必须由服务端添加不可覆盖的 `must` 条件：

```json
{
  "must": [
    {"key": "approved", "match": {"value": true}},
    {"key": "claim_eligibility", "match": {"value": true}},
    {"key": "storage_permission", "match": {"value": true}},
    {"key": "review_status", "match": {"value": "ingested"}},
    {"key": "sensitive_information_level", "match": {"any": ["public", "controlled"]}},
    {"key": "secret_exposure_rank", "range": {"lte": "<server-side-clearance>"}}
  ],
  "must_not": [
    {"key": "confidence_label", "match": {"value": "insufficient"}}
  ]
}
```

实现状态：`KnowledgeSearchFilter` 只暴露领域、ontology、证据通道和图索引过滤；不暴露安全字段。`QdrantService._build_filter()` 无论调用者是否提供可选 filters，都会加入本节硬条件。没有授权上下文时默认只允许 `public/controlled` 且 `secret_exposure_rank <= 1`。

## 10. 检索路由与排序

### 10.1 两阶段检索

1. **意图路由**：识别用户在问效果体验、方法诊断、技术执行、心理机制、证据，还是表演设计；同时识别表演 domain 和可用安全级别。若需语义分类，只使用现有 GLM，不增加 LLM provider。
2. **分通道向量检索**：对多个 `knowledge_type / evidence_level / ontology_paths` 组合分别查询，全部带第 9 节硬过滤，再用 Reciprocal Rank Fusion 或显式通道优先级合并。不同通道的 cosine score 不直接相加。

排序依次考虑：

1. 与问题和表演上下文的语义相关性。
2. 路由优先级（例如诊断问题先科学机制，再实践应用）。
3. 同一证据通道内的 confidence、证据直接性和适用条件。
4. 是否有未解决反证；有则降权并在答案中显式呈现。
5. 来源多样性，避免同一来源或同一数据集占满结果。

`review` 不因年代新而自动优于 `empirical`，`practitioner` 不因措辞相似而压过科学证据；这些是不同通道，需要分别标注。

### 10.2 示例：“为什么观众没有注意到我的秘密动作？”

路由器先收集必要上下文（秘密动作位置、时机、视线/语言、观众距离、是否事后才暴露），然后按以下顺序检索：

1. **Psychology evidence**
   - `knowledge_type in [evidence, psychology]`
   - `knowledge_origin=scientific_evidence`
   - `evidence_level in [review, empirical]`
   - `ontology_paths` 优先 `psychology.attention.*`、`psychology.expectation.*`
   - 目标：说明“未报告动作”可能对应哪些机制、证据边界是什么。
2. **Misdirection principles**
   - `knowledge_type in [psychology, performance]`
   - `ontology_paths` 优先 `misdirection.spatial`、`temporal`、`social`、`cognitive`、`emotional`
   - 目标：把机制映射为空间、时间、社会或认知控制条件。
3. **Practitioner applications**
   - `knowledge_origin=expert_practice`
   - `evidence_level=practitioner`
   - `knowledge_type=performance`
   - 目标：给出清楚标为实践知识的处理建议，不称其为实验结论。
4. **Technique examples**
   - `knowledge_type=technique`
   - `domain` 与用户场景相交
   - 仅在 clearance 允许时包含 `method_detail`；否则停留在一般风险与练习建议。

回答组装顺序保持这四层，并附：证据通道、confidence、适用条件、limitations、可复核 citation/locator 和已知反证。系统应说“这几种机制与条件相符”，而不是仅凭一次检索断言观众确实经历了某特定机制。

## 11. 与现有 Qdrant payload 的兼容和迁移

现有 `KnowledgeChunk.to_payload()` 已提供：

- `text`, `chunk_id`, `chunk_index`, `heading`, `page_number`, `source_locator`
- 文档级 `category`, `technique`, `psychology`, `performer`
- 分块级 `magic_category`, `techniques`, `psychological_principles`, `performers`, `sources`
- `entity_ids`, `entity_types`, `relation_types`

它们可作为 provenance 和路由输入，但当前 point 粒度仍是 document chunk。迁移原则：

| 当前字段 | v0.1 目标 |
|---|---|
| `chunk_id` | 保留为来源 span 审计 ID，不再充当知识单元身份 |
| `chunk_index`, `heading`, `page_number`, `source_locator` | 保留在 Evidence 来源定位中 |
| `category/magic_category` | 规范化为 `domain` 与 `ontology_paths`；迁移期可保留别名 |
| `technique/psychology/performer` | 解析为稳定 entity IDs；名称数组只作显示/兼容 |
| `metadata_confidence` | 映射为 extraction confidence，绝不映射到 `confidence` |
| `entities/relationships` | 保留 canonical ID；新增 relation IDs 和 evidence card links |
| raw `text` | 不迁移；改为人工批准的结构化检索文本 |

迁移必须新建或重建 collection；不得把历史 document chunk 仅补一个 `approved=true` 后继续使用，因为那仍然违反“原始 PDF 不直接作为知识”的边界。

## 12. 未来 Knowledge Graph 兼容

本设计不添加图数据库。未来图集成时：

- `entity_ids` 与 `relationship_ids` 是图节点/边的稳定连接键。
- `evidence_card_id` 是边和节点的 provenance 键；关系不能只有自由文本 evidence。
- `canonical_claim_id` 可成为 claim node 或 statement identity，而无需改变 Qdrant point。
- `ontology_paths` 负责当前层级过滤，未来可由图遍历计算但不作为图的 source of truth。
- Qdrant 返回向量候选，图层负责邻居扩展、关系约束和路径解释；两者通过现有 storage-neutral models/ports 组合。
- 图写入仍需复用相同的人工批准、storage permission、反证和敏感信息规则。

因此，Qdrant collection 可以重建，Evidence Cards、canonical entities、relationships 和审核审计记录才是不可丢失的知识来源。
