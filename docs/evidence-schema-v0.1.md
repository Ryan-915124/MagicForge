# MagicForge Evidence Schema v0.1

## 1. 目的与边界

Evidence Layer 的职责不是保存“论文内容”，而是保存**可审查、可定位、可反驳的原子主张**。它位于来源处理与领域知识之间：

```text
已批准来源
  -> 带定位的抽取结果
  -> Evidence Card（逐条主张审核）
  -> 已批准的 Knowledge Node / Relationship
  -> 检索投影
```

必须始终保持三个独立判断：

1. `source approval`：允许访问和抽取某个来源；不代表其中内容正确。
2. `claim approval`：审核员确认某条主张被来源支持、定位准确且分类正确；不代表允许存储。
3. `storage permission`：允许把已批准主张或其知识投影写入 Qdrant；不由来源批准自动推导。

搜索摘要、未核验元数据、整篇 PDF、PDF 原始分块和 GLM 的自由推断都不是 Evidence Card。GLM 只能提出候选卡片，不能批准卡片。

本设计扩展但不替换当前模型：

- `ResearchCandidate` 仍表示未核验候选，不能入库。
- `CitationRecord` 仍负责来源身份、访问时间、同行评审状态和核验记录。
- `ExtractedClaim` 仍是某一来源分块上的抽取候选。
- `KnowledgeProposal` 仍是可审核提案，不是写入请求。
- `ReviewItem` 的 `pending / approved / rejected / ingested` 状态机仍是存储闸门。

## 2. 三条认识论通道

每张 Evidence Card 必须声明 `knowledge_origin`。三条通道可以在回答中并列，但不得混合计分或互相冒充。

| `knowledge_origin` | 可证明什么 | 不可据此证明什么 | 典型 `evidence_level` |
|---|---|---|---|
| `scientific_evidence` | 受研究设计约束的认知、行为或体验主张 | 某个表演处理一定适合所有观众与场景 | `empirical`, `review` |
| `expert_practice` | 某位实践者采用、主张或反复观察到的表演方法；经过审核的实践建议 | 普遍心理机制或因果效应 | `practitioner`，少数个案为 `anecdotal` |
| `personal_interpretation` | 明确署名的分析、假设或跨来源解释 | 外部事实、科学共识或实践者原意 | `anecdotal` |

实践者著作可以支持“作者建议在秘密动作与效果之间制造时间距离”，但仅凭该著作不能支持“时间距离已被实验确认会提高欺骗成功率”。后者必须由科学通道中的卡片独立支持。

历史陈述同样遵循此原则：原始节目单可以支持“某节目在某时某地被宣传”，不能自动支持该节目使用了某秘密方法。详细类别保留在 `evidence_class` 中，粗粒度检索等级仍使用规定的四个 `evidence_level`。

## 3. Evidence Card

一张卡片表示“**一个来源中的一个可定位证据单元，对一个原子主张的支持或反驳**”。同一主张由多个来源支持时，应保留多张卡片，再通过 `canonical_claim_id` 聚合；不得把多个来源拼成一张卡片。

### 3.1 规范字段

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 | 稳定 ID；由规范化主张、`citation_id`、定位及版本生成 |
| `schema_version` | string | 是 | 本版固定为 `evidence-0.1` |
| `canonical_claim_id` | UUID | 是 | 指向规范化主张；同义措辞可共享该 ID |
| `claim` | string | 是 | 单一、可判真伪、保留适用条件的陈述 |
| `claim_polarity` | enum | 是 | `supports`, `contradicts`, `qualifies` |
| `mechanism_ids` | UUID[] | 是 | 指向 `CognitiveMechanism`；没有明确证据时为空，不猜测 |
| `mechanism_status` | enum | 是 | `linked`, `unresolved`, `not_applicable`；禁止用猜测填满空 ID |
| `principle_ids` | UUID[] | 否 | 指向 `PsychologyPrinciple` |
| `applicable_domain` | enum[] | 是 | `card`, `close-up`, `stage`, `mentalism`, `theory` 中至少一个 |
| `magic_application` | string | 否 | 来源明确陈述或经审核的应用解释；用 `application_origin` 标注来源 |
| `application_origin` | enum | 是 | `source_stated`, `reviewer_synthesis`, `not_applicable` |
| `knowledge_origin` | enum | 是 | `scientific_evidence`, `expert_practice`, `personal_interpretation` |
| `evidence_class` | enum | 是 | 细粒度证据/知识类别，见 3.2 |
| `evidence_level` | enum | 是 | 检索用粗粒度等级：`empirical`, `review`, `practitioner`, `anecdotal` |
| `source` | object | 是 | 必须含 `citation_id`, `source_id`, `source_candidate_id` |
| `locator` | object | 是 | 精确到页、节、段、图表或时间码；见第 4 节 |
| `evidence_excerpt` | string | 是 | 供审核的最小必要上下文；不得用搜索摘要代替 |
| `excerpt_hash` | string | 是 | 对规范化片段计算摘要，用于审计和变更检测 |
| `limitations` | string[] | 是 | 没有已知限制时写明审核范围，不能让 GLM 填“无”即通过 |
| `population_context` | string | 否 | 研究样本或实践观众条件 |
| `performance_context` | string | 否 | 道具、距离、场地、观看角度、互动等边界 |
| `confidence` | object | 是 | 证据置信度，见第 5 节 |
| `extraction_confidence` | float | 是 | `0..1`；仅表示抽取与原文吻合程度，不表示主张为真 |
| `contradiction_status` | enum | 是 | `not_checked`, `none_found`, `resolved`, `unresolved` |
| `contradicting_evidence_ids` | UUID[] | 是 | 指向反证或限定卡片；允许空数组 |
| `supersedes` | UUID[] | 是 | 新版本取代的旧卡片，允许空数组 |
| `review` | object | 是 | claim-level 审核记录，见第 7 节 |
| `created_at` | datetime | 是 | 候选卡片创建时间 |
| `created_by` | string | 是 | 抽取器/人工录入者标识；不得冒充 reviewer |

`magic_application` 若由审核员跨域推导，必须使用 `application_origin=reviewer_synthesis`，且推导本身应生成独立的 `personal_interpretation` 卡片或知识节点，不能改写科学来源原意。

### 3.2 `evidence_class`

细粒度类别负责保留来源性质，`evidence_level` 只负责粗粒度过滤。

| `evidence_class` | `knowledge_origin` | `evidence_level` | 说明 |
|---|---|---|---|
| `controlled_experiment` | `scientific_evidence` | `empirical` | 有对照/操纵的实验 |
| `quasi_experiment` | `scientific_evidence` | `empirical` | 非完全随机或自然实验设计 |
| `observational_study` | `scientific_evidence` | `empirical` | 观察、相关或现场研究 |
| `systematic_review` | `scientific_evidence` | `review` | 有明确检索与筛选方法的综述 |
| `meta_analysis` | `scientific_evidence` | `review` | 定量汇总 |
| `narrative_review` | `scientific_evidence` | `review` | 非系统性学术综述，置信上限低于系统综述 |
| `expert_instruction` | `expert_practice` | `practitioner` | 实践者明确给出的操作或表演建议 |
| `expert_case_analysis` | `expert_practice` | `practitioner` | 对具体 routine/performance 的专家分析 |
| `practitioner_report` | `expert_practice` | `anecdotal` | 未系统验证的经验报告 |
| `historical_primary_record` | `expert_practice` | `anecdotal` | 节目单、信件、同期记录；只支持其直接记载 |
| `historical_secondary_analysis` | `expert_practice` | `practitioner` | 有来源链的历史研究或实践史分析 |
| `analyst_interpretation` | `personal_interpretation` | `anecdotal` | MagicForge 审核员/分析者的解释 |
| `anecdotal_observation` | `personal_interpretation` | `anecdotal` | 个体观察或未验证印象 |

“同行评审”是来源属性，不等同于 `evidence_class`，也不会自动产生高置信度。预印本、同行评审论文、书籍章节都必须根据实际研究设计分类。

### 3.3 原子主张规则

卡片的 `claim` 必须：

- 只包含一个中心断言；因果、相关和描述性陈述不可混写。
- 保留原研究或来源中的人群、任务、材料和情境边界。
- 区分“未注意到”“未记住”“未报告”和“错误解释”；它们不是同一结果。
- 不把统计显著性改写成实际效果很强，也不从无差异推导“完全没有作用”。
- 不把实践者的规范性建议改写为科学事实。
- 不从效果名称反推秘密方法，不从表演者归属反推创造者归属。
- 无法由最小证据片段和定位直接核查时，`claim_eligibility=ineligible`。

## 4. 来源身份与定位

`source` 对象复用现有来源模型的稳定身份：

```yaml
source:
  citation_id: <CitationRecord.id>
  source_id: <KnowledgeEntity Source.id>
  source_candidate_id: <ResearchCandidate.id>
  research_paper_id: <ResearchPaper.id-or-null>
  citation_status: full_text_verified
  peer_review_status: peer_reviewed
```

要求：

- `citation_id` 必须指向 `CitationRecord`，并与 `source_candidate_id` 一致。
- 科学主张至少需要可核验的正文访问；`search_snippet` 不具备 claim eligibility。
- `metadata_verified` 只证明书目信息，不证明正文主张；高置信科学卡片通常要求 `full_text_verified`。
- `Source` 与 `ResearchPaper` 节点来自已核验 provenance，不允许 GLM 自行创建。这与当前 `ExtractedEntity` 校验保持一致。

`locator` 是结构化对象，而不是模糊备注：

```yaml
locator:
  media_type: pdf
  page_number: 7
  section: Results
  paragraph: 3
  figure_or_table: null
  timestamp_start: null
  timestamp_end: null
  source_locator: "page 7, Results, paragraph 3"
```

定位规则：

- PDF：PDF 页码必填；若印刷页码不同，可同时记录 `printed_page`。
- Markdown/Text：标题路径和稳定段落序号必填；若文件可变，记录内容版本/hash。
- Web：规范 URL、页面标题、访问时间和标题/段落路径必填。
- 音视频转录：起止时间码和转录版本必填。
- 书籍：版本/版次、章节和页码必填。

`evidence_excerpt` 只保留审核所需的最小上下文，并遵守版权与存储许可。它留在 Evidence/Review 存储中；默认不作为 Qdrant 的 embedding 文本，也不把整页或整段论文复制为知识。

## 5. Confidence 体系

### 5.1 两种 confidence 不得混用

- `extraction_confidence`：抽取器认为字段是否忠实于来源。当前 `ExtractedClaim.confidence` 映射到这里。
- `confidence`：审核后，该证据对**这条限定后的主张**提供多强支持。

GLM 可以给出前者的候选值，不能决定后者。证据置信度由人工审核结果和透明量表生成。

### 5.2 评分维度

每个维度由审核员给 `0.0 / 0.5 / 1.0`，并附一句理由：

| 维度 | 0.0 | 0.5 | 1.0 |
|---|---|---|---|
| `provenance_quality` | 身份/正文不可核验 | 部分核验或来源有限 | 身份、版本、正文和定位均核验 |
| `method_rigor` | 无可评估方法 | 方法有限或实践经验 | 设计适合该主张且报告充分 |
| `claim_directness` | 主要由分析者推断 | 间接支持 | 来源直接测试或明确陈述该主张 |
| `consistency` | 有强且未解释的反证 | 证据有限/混合 | 独立证据一致或高质量综述支持 |
| `magic_applicability` | 与表演情境距离很远 | 有合理迁移但边界明显 | 直接在相近魔术/表演条件下观察 |

`score` 为五项等权平均，保存到 `0..1`，用于同类卡片的透明排序；它不是“真理概率”。最终 `label` 还必须服从证据上限和反证规则：

| `label` | 基础分数 | 含义 |
|---|---:|---|
| `insufficient` | `< 0.40` | 仅供复核，不得生成可检索知识 |
| `low` | `0.40–0.59` | 有限支持，回答时必须突出限制 |
| `moderate` | `0.60–0.79` | 有直接但仍受边界限制的支持 |
| `high` | `>= 0.80` | 多个独立直接证据或高质量综合证据，且无重大未解决反证 |

强制上限：

- 单一实践者经验对“实践建议”最高为 `moderate`，对科学机制没有科学 confidence。
- `personal_interpretation` 最高为 `low`，无论措辞多有说服力。
- 仅有摘要、搜索摘要或缺少精确定位时为 `insufficient`，不可批准。
- 单项独立实验通常最高为 `moderate`；达到 `high` 需要独立重复、证据综合或等价的强依据。
- `contradiction_status=unresolved` 时最高为 `moderate`；若反证直接否定核心主张，应降为 `low` 或拒绝。
- 置信度只在相同 `knowledge_origin` 与相同主张范围内比较；实践卡片不能抬高科学卡片分数。

推荐结构：

```yaml
confidence:
  score: 0.72
  label: moderate
  dimensions:
    provenance_quality: {score: 1.0, reason: "..."}
    method_rigor: {score: 0.5, reason: "..."}
    claim_directness: {score: 1.0, reason: "..."}
    consistency: {score: 0.5, reason: "..."}
    magic_applicability: {score: 0.5, reason: "..."}
  assessed_by: <reviewer-id>
  assessed_at: <datetime>
```

## 6. 反证与限定条件

反证不是自由文本备注，而是 Evidence Card 之间的显式关系：

- `contradicts`：在相同或足够相近条件下得到相反结果。
- `qualifies`：主张只在更窄条件下成立，或效应大小/范围受到限制。
- `replicates`：独立数据在相近条件下支持同一主张。
- `uses_same_dataset`：论文不同但数据不独立，聚合时不得重复计数。

每次 claim approval 前必须记录一次反证检查。`none_found` 表示“在记录的范围和日期内未找到”，不表示不存在反证。检查至少记录检索范围、检查日期和审核员；详细过程可以保存在审核审计日志中。

当新反证出现时：

1. 不删除旧卡片。
2. 新建反证/限定卡片并建立关系。
3. 将 canonical claim cluster 标为 `needs_resynthesis`。
4. 重新评估 cluster confidence。
5. 如旧综合结论失效，以新版本 `supersedes` 旧版本，并让旧 Qdrant 投影退出检索。

## 7. Claim-level 审核

每张卡片的审核对象独立于来源审核，也独立于同一 `KnowledgeProposal` 中的其他卡片：

```yaml
review:
  claim_eligibility: not_assessed
  extraction_permission: none
  storage_permission: none
  approved: false
  review_status: pending
  reviewer: null
  review_date: null
  approval_reason: null
  contradicting_evidence_checked: not_checked
  sensitive_information_level: controlled
secret_exposure_level: general_principle
```

`review_status` 使用当前审核语义：`pending`, `approved`, `rejected`, `ingested`。其中：

- `claim_eligibility` 使用 `not_assessed`, `eligible`, `eligible_with_limits`, `ineligible`；只有两个 `eligible*` 值可投影为 Qdrant 的 `claim_eligibility=true`。
- `extraction_permission` 使用 `none`, `metadata_only`, `selected_sections`, `full_text`，并继承来源审批的实际范围。
- `approved=true`：命名审核员批准了这张卡片；不得由置信度阈值自动设置。
- `storage_permission` 使用 `none`, `derived_knowledge_only`, `derived_with_short_excerpt`；后两个值只有在目标投影落入批准范围时，才映射为 Qdrant 的 `storage_permission=true`。
- `sensitive_information_level` 使用 `public`, `controlled`, `secret_method`, `restricted`，表示治理与访问等级；`secret_exposure_level` 使用 Qdrant Schema 的内容暴露粒度，二者不得互换。
- `contradicting_evidence_checked` 使用 `not_checked`, `checked_none_found`, `checked_conflicts_linked`, `not_applicable`；可检索主张不得保持 `not_checked`。
- `ingested`：只在完整写入成功后由当前审核服务记录。
- 修改已批准卡片的 claim、locator、source 或限制条件必须创建新版本并重新审核。

批准前最低检查项：来源与版本一致、定位可复现、片段忠实、主张原子化、认识论通道正确、机制不是臆测、限制条件完整、反证已检查、敏感级别正确、存储许可明确。

## 8. 跨卡片聚合规则

聚合单位是 `canonical_claim_id`，聚合产物是可版本化的 `Claim Synthesis`，而不是覆盖原卡片。

1. **先对齐主张范围。** 不同人群、观看条件、因变量或因果方向的卡片不能直接合并。
2. **去除非独立重复。** DOI、数据集、作者说明和 `uses_same_dataset` 用于识别重复报告。
3. **科学与实践分轨。** 分别生成 `scientific_summary`、`practice_summary` 和 `interpretation_summary`；总览可并列展示，但不合成一个分数。
4. **综述避免双计数。** 若系统综述已包含某实验，展示时可以同时引用，但置信计算不能把它们当作两个独立数据源。
5. **反证必须可见。** 同时报告支持、反驳和限定卡片的数量、独立性与适用条件，不做简单多数投票。
6. **不平均 GLM 分数。** `extraction_confidence` 永远不参与证据综合。
7. **取最窄有效结论。** 当证据只支持局部条件时，综合主张必须收窄，而不是降低措辞后继续泛化。
8. **应用是第二层推理。** 从认知研究映射到魔术实践时，保留原科学主张，并新建带来源的应用解释；两者不能合并成同一事实。
9. **每次综合可追溯。** 保存纳入/排除的 card IDs、理由、规则版本、审核员和时间。

`Claim Synthesis` 达到 `approved=true`，且 `storage_permission` 为适用于目标投影的 `derived_knowledge_only` 或 `derived_with_short_excerpt` 后，才可投影为 `PsychologyPrinciple`、`CognitiveMechanism`、其他 Knowledge Node 或证据点。单张卡片的批准不会自动批准由它生成的所有实体和关系。

## 9. 与现有模型的映射及实现缺口

| 现有字段/模型 | Evidence v0.1 含义 | 后续实现要求 |
|---|---|---|
| `ExtractedClaim.statement` | 候选 `claim` | 原子化校验后才能建卡 |
| `ExtractedClaim.evidence_excerpt` | 候选 `evidence_excerpt` | 必须与 locator 一起由人复核 |
| `ExtractedClaim.locator` | 候选 `locator.source_locator` | 升级为结构化 locator |
| `ExtractedClaim.confidence` | `extraction_confidence` | 不得映射为证据 confidence |
| `ResearchExtractionResult.limitations/conflicts` | 卡片限制与反证候选 | 需逐条归属，不能只留文档级列表 |
| `CitationRecord` | `source` 的权威引用记录 | 保留核验状态和验证证据 |
| `KnowledgeRelationship.evidence/source_chunk_id` | 边的来源线索 | 后续增加 `evidence_card_ids` 投影 |
| `ReviewItem` | 提案级审核闸门 | 后续支持卡片级决策，但不得放宽现有规则 |

本文只定义目标架构，不修改这些运行时模型。

## 10. 示例（仅展示结构，不是已批准知识）

以下沿用任务中给出的 Kuhn 等示例。ID、定位、评分和状态均为占位符；它不表示 MagicForge 已核验、批准或入库该来源。

```yaml
id: <evidence-card-uuid>
schema_version: evidence-0.1
canonical_claim_id: <claim-uuid>
claim: "在特定魔术观看任务中，观众可能未报告一个意外事件。"
claim_polarity: supports
mechanism_ids: [<inattentional-blindness-entity-id>]
mechanism_status: linked
principle_ids: [<selective-attention-principle-id>]
applicable_domain: [theory]
magic_application: "可作为注意控制型误导的候选解释，但不能单独证明任一秘密动作必然不被察觉。"
application_origin: reviewer_synthesis
knowledge_origin: scientific_evidence
evidence_class: controlled_experiment
evidence_level: empirical
source:
  citation_id: <verified-citation-id>
  source_id: <source-entity-id>
  source_candidate_id: <candidate-id>
  research_paper_id: <research-paper-id>
  citation_status: <must-be-verified>
  peer_review_status: <must-be-verified>
locator:
  media_type: pdf
  page_number: <must-be-filled>
  section: <must-be-filled>
  paragraph: <must-be-filled>
  source_locator: <must-be-filled>
evidence_excerpt: <minimum-review-context-must-be-filled>
excerpt_hash: <must-be-computed>
limitations:
  - "结论仅适用于来源实际测试的任务、样本和报告指标。"
confidence:
  score: null
  label: insufficient
  dimensions: {}
extraction_confidence: 0.0
contradiction_status: not_checked
contradicting_evidence_ids: []
supersedes: []
review:
  claim_eligibility: not_assessed
  extraction_permission: none
  storage_permission: none
  approved: false
  review_status: pending
  reviewer: null
  review_date: null
  approval_reason: null
  contradicting_evidence_checked: not_checked
  sensitive_information_level: controlled
secret_exposure_level: general_principle
```

该示例故意不能通过 ingestion gate：它说明架构如何表达“待核验”，而不是制造一条看似完整的知识。
