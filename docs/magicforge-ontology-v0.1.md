# MagicForge Knowledge Ontology v0.1

状态：设计稿，不触发抽取、审核、嵌入或入库。

本文定义 MagicForge 的领域本体，目标是表示“魔术如何成为一种人的体验”，而不是表示“资料存放在哪里”。本体位于现有 Magic Knowledge Schema v0.3 之上：本文的 `v0.1` 是本体文档版本，并非运行时 `schema_version = 3.0` 的降级或替代。

## 1. 设计边界

本体需要同时表达：

- 观众看见、相信和感受到什么；
- 秘密方法与可训练技巧如何产生该体验；
- 注意、记忆、预期和社会认知如何参与解释；
- 误导与表演设计如何组织时间、空间和观众关系；
- 技能如何形成、练习和组合成完整流程；
- 知识来自何处、属于科学证据、专家实践还是个人解释；
- 效果、方法与表演传统如何随时间演化。

本版本遵守以下不变量：

1. 不把原始论文、PDF 页面或书籍段落直接当成知识。
2. 来源获准、主张获准和知识入库是三个不同决策。
3. 科学机制、实践原则和个人解释不得因主题相似而合并。
4. 实践者著作可以记录“谁提出或采用了什么”，但不能自动证明心理机制或效果有效。
5. 方法细节和训练步骤继承来源权限及敏感度限制；存在节点不等于允许向所有用户展示其全部属性。
6. GLM 可以提出实体、关系和分类候选，但不能自行赋予其已审核状态。
7. 本体不依赖图数据库。Qdrant 仍是当前向量存储，未来图适配器只消费稳定节点与关系记录。

## 2. 三层认知架构

```text
Audience experience
└── Effect
    ├── audience perception
    ├── emotional impact
    └── apparent sequence

Secret and performance construction
├── Method
├── Technique
├── Misdirection profile
├── Performance design
└── Expertise and training

Human explanation and provenance
├── Psychology Principle        performance-domain interpretation
├── Cognitive Mechanism         scientific explanatory construct
├── Performer                   person/tradition context
├── Source                      accessed artifact/version
└── Research Paper              scholarly work identity
```

这三层不可压成单一文档标签。例如，“观众没注意到动作”至少可能涉及一个 `Technique`、一个误导画像、一个 `PsychologyPrinciple`、一个或多个 `CognitiveMechanism`，以及分别支持这些主张的证据卡。

## 3. 与当前规范模型的映射

现有代码定义八种规范实体和六种规范关系。本体 v0.1 不新增运行时枚举，而是在这些稳定实体上添加受控画像。

| 本体概念 | 当前规范实体 | v0.1 表达方式 |
|---|---|---|
| 效果 | `Effect` | 直接实体 |
| 秘密方法 | `Method` | 直接实体 |
| 执行技巧 | `Technique` | `technique_family = execution` 等画像 |
| 心理原则 | `PsychologyPrinciple` | 直接实体 |
| 科学认知机制 | `CognitiveMechanism` | 直接实体 |
| 误导类型 | `PsychologyPrinciple` | `principle_family = misdirection` 与 `misdirection_type` |
| 表演设计概念 | `Technique` | `technique_family = performance_design` 与 `performance_facet` |
| 练习法、创作法、流程构建技能 | `Technique` | `technique_family = training_method / creativity / routine_construction` |
| 专家认知 | `CognitiveMechanism` | `expertise_stage = expert`，必须由科学证据卡支持相关主张 |
| 表演者 | `Performer` | 直接实体 |
| 来源载体 | `Source` | 直接实体 |
| 学术作品 | `ResearchPaper` | 直接实体，并与访问到的 `Source` 区分 |

所有实体继续使用当前 `KnowledgeEntity` 的公共字段：

- `id`：按实体类型和规范化名称生成的稳定 UUID；
- `type`：八种规范 `EntityType` 之一；
- `name`：规范名称；
- `description`：不泄露受限秘密的简要说明；
- `aliases`：经过实体消歧的别名；
- `attributes`：下文各画像的类型化字段。

在字段尚未被提升为专用 Pydantic 模型前，逻辑字段写入 `attributes`。建议每个画像携带：

```yaml
ontology_version: "0.1"
ontology_domains: [audience_experience]
profile: effect
```

受控值使用小写 `snake_case`。`unknown` 表示尚不知道；`other` 表示已知但不在当前词表，必须附审核说明。不得用任意自由文本悄悄扩展枚举。

## 4. 分层本体

### A. Effect：观众体验到的效果

`Effect` 描述观众层面的事件，不包含秘密解法。即使两个处理使用不同方法，只要观众体验的核心事件相同，也可以指向同一效果实体；当体验结构、情绪目标或叙事含义有实质差异时，再建立明确的效果变体。

#### A.1 受控分类

- `appearance`
- `vanish`
- `transformation`
- `prediction`
- `restoration`
- `impossible_knowledge`
- `transposition`
- `penetration`
- `levitation`
- `animation`
- `multiplication`
- `coincidence`
- `mind_reading`
- `control_or_escape`
- `other`
- `unknown`

类别允许多值，但必须有一个 `primary_effect_category`。例如一段流程可以主要是 `transposition`，同时包含 `vanish` 子阶段。

#### A.2 字段

| 字段 | 类型 | 约束与含义 |
|---|---|---|
| `effect_name` | string | 映射到实体 `name`，不得包含方法泄露 |
| `description` | string | 观众可见事件的中性概述 |
| `primary_effect_category` | enum | 必填，取上述受控值 |
| `secondary_effect_categories` | enum[] | 可空、去重 |
| `audience_perception` | string | 观众被设计为相信发生了什么，而非实际方法 |
| `emotional_impact` | enum[] | `wonder`、`surprise`、`tension`、`relief`、`amusement`、`mystery`、`empathy`、`unease`、`other`、`unknown` |
| `apparent_sequence` | object[] | 仅记录观众可感知的阶段与顺序 |
| `audience_conditions` | object | 人数、视角、距离、参与方式等条件；未知值显式保留 |
| `common_method_ids` | UUID[] | 从审核后的 `uses` 关系派生，不作为第二真相源手工维护 |
| `related_principle_ids` | UUID[] | 从审核后的 `uses` / `explains` 关系派生 |
| `variant_scope` | enum | `canonical`、`routine_specific`、`performer_specific`、`historical_variant` |

“效果强度”不是 `Effect` 的永久事实；它随表演、观众和场景变化，应由 Magic Theory Analyzer 在具体上下文中评估。

### B. Method：秘密实现方式

`Method` 表示秘密因果方案；它回答“真实发生了什么”，但不等同于完成该方案所需的手上动作或表演能力。一个方法可以使用多个技巧，一个效果也可以由多个替代方法实现。

#### B.1 受控分类

- `sleight`
- `gimmick`
- `secret_action`
- `psychological_method`
- `information_control`
- `mechanical_method`
- `mathematical_method`
- `dual_reality`
- `confederate`
- `mixed`
- `other`
- `unknown`

#### B.2 字段

| 字段 | 类型 | 约束与含义 |
|---|---|---|
| `method_name` | string | 映射到实体 `name` |
| `method_family` | enum | 必填 |
| `secret_summary` | string | 受权限控制的秘密概述 |
| `required_skill` | enum | `beginner`、`intermediate`、`expert`、`mixed`、`unknown` |
| `required_technique_ids` | UUID[] | 从 `requires` / `uses` 关系派生 |
| `visibility_risk` | enum | `low`、`medium`、`high`、`context_dependent`、`unknown` |
| `timing_requirements` | object | 秘密动作相对表面动作和揭示时刻的位置 |
| `audience_conditions` | object | 角度、距离、观众数量、检查权和互动约束 |
| `failure_modes` | object[] | 可观察症状、原因与缓解条件；不是泛化评分 |
| `secret_exposure_level` | enum | 由安全策略定义；控制展示与检索，不代表证据质量 |

同一方法的公开摘要和秘密细节可以属于同一稳定节点，但必须作为不同权限字段处理。不得为绕开权限而创建名称不同、实为同一方法的影子节点。

### C. Technique：表演层可训练技能

`Technique` 是可执行、可练习、可观察错误的能力。它可以服务于秘密动作，也可以服务于注意管理、表演设计、训练或流程构建。

#### C.1 受控技巧族

- `execution`
- `concealment`
- `transfer_or_switch`
- `steal_or_load`
- `information_management`
- `attention_management`
- `audience_management`
- `performance_design`
- `training_method`
- `creativity`
- `routine_construction`
- `other`
- `unknown`

Palm、False Transfer、Retention Vanish、Switch、Steal、Load 通常属于前四类；Misdirection 作为实际执行能力可属于 `attention_management`，其理论分类则由专门的误导画像表达。这样可避免把“执行什么”和“为什么可能有效”混成一个节点。

#### C.2 字段

| 字段 | 类型 | 约束与含义 |
|---|---|---|
| `technique` | string | 映射到实体 `name` |
| `technique_family` | enum | 必填 |
| `execution_steps` | object[] | 有序步骤；继承来源的抽取许可和敏感等级 |
| `required_conditions` | object | 道具、位置、角度、节奏、观众状态等 |
| `prerequisite_technique_ids` | UUID[] | 从 `requires` 关系派生 |
| `common_mistakes` | object[] | 错误表现、可能原因、纠正线索 |
| `expert_variations` | object[] | 变体名称、适用条件、出处与审核状态 |
| `minimum_proficiency` | enum | `beginner`、`intermediate`、`expert`、`all_levels`、`unknown` |
| `assessment_cues` | string[] | 可由教练或学习者观察的执行指标 |
| `context_limits` | string[] | 不可安全泛化的条件 |

执行步骤不能只因出现在获准来源中就被批准；它们仍需经过主张和敏感信息审核。

### D. Psychological Principles：心理原则

`PsychologyPrinciple` 是魔术领域可应用的解释或设计原则。它不是研究结果本身，也不应以一个无条件置信度概括所有情境。相关科学解释应由独立的 `CognitiveMechanism` 节点和证据卡承载。

#### D.1 必需层级

```text
psychological_principle
├── attention
│   ├── overt_attention
│   ├── covert_attention
│   ├── selective_attention
│   ├── inattentional_blindness
│   └── change_blindness
├── memory
│   ├── false_memory
│   ├── reconstruction
│   └── encoding_limitations
├── expectation
│   ├── prediction
│   ├── assumption
│   ├── surprise
│   └── violation_of_expectation
└── social_cognition
    ├── trust
    ├── gaze_following
    ├── social_cues
    └── intent_attribution
```

此树是初始受控词表，不表示子项互斥。例如一次处理可以同时涉及选择性注意、假设和视线跟随。

#### D.2 字段

| 字段 | 类型 | 约束与含义 |
|---|---|---|
| `principle_name` | string | 映射到实体 `name` |
| `principle_family` | enum | `attention`、`memory`、`expectation`、`social_cognition`、`misdirection` |
| `principle_category` | enum | 取上述叶节点；误导原则取 E 节的分类 |
| `definition` | string | 术语的边界化定义 |
| `mechanism_ids` | UUID[] | 指向 `CognitiveMechanism`，由 `explains` 关系派生 |
| `mechanism_summary` | string | 面向应用者的摘要，不替代科学机制节点 |
| `magic_application` | object[] | 适用的效果、方法或技巧及必要条件 |
| `supporting_evidence_card_ids` | UUID[] | 仅引用已通过主张审核的证据卡 |
| `limitations` | string[] | 范围、替代解释、生态效度与已知不确定性 |
| `contradicting_evidence_card_ids` | UUID[] | 不得隐藏相反或限制性发现 |

#### D.3 Cognitive Mechanism

`CognitiveMechanism` 用于科学解释层，至少包括：

- `definition`：科学构念的操作性边界；
- `mechanism_domain`：如 attention、memory、expectation、social_cognition、motor_expertise；
- `operationalization`：研究中如何测量或操纵；
- `boundary_conditions`：人群、任务和情境限制；
- `supporting_evidence_card_ids`；
- `contradicting_evidence_card_ids`；
- `open_questions`。

允许“一个机制解释多个原则”和“一个原则由多个机制共同解释”。若仅有实践者解释，应保留为实践主张，不得创建一个看似已证实的科学机制。

### E. Misdirection Model：专门的误导本体

误导是跨越心理原则和表演技巧的组合模型，而不是单一技巧同义词。每个误导概念以 `PsychologyPrinciple` 为规范节点，使用 `principle_family = misdirection`；具体如何执行则以 `Technique` 表示。

| `misdirection_type` | 操作定义 | 可能关联的机制范围 | 典型必要条件 | 常见失败模式 |
|---|---|---|---|---|
| `spatial` | 将注意或视线从秘密动作位置移开 | 空间定向、选择性注意、视线线索 | 存在可接受的空间目标；秘密区不成为高显著性区域 | 秘密区动作过大；引导目标缺乏动机；观众视角不同 |
| `temporal` | 拉开方法发生与效果被理解之间的时间距离 | 事件分段、编码限制、记忆重建 | 延迟具有表演动机；因果线索不被重新连接 | 延迟机械或过短；流程反而提醒观众回溯 |
| `social` | 通过视线、声音、互动和社会关系引导观众 | 视线跟随、社会线索、信任、意图归因 | 互动自然且与角色一致；观众愿意参与 | 过度盯视；语言与动作冲突；信任不足或观众抵抗 |
| `cognitive` | 控制假设、任务模型与事后解释 | 预期、假设、预测、重建 | 表面因果链清晰；替代解释在当下不显著 | 说辞强调秘密条件；假设与观众先验冲突 |
| `emotional` | 以故事、幽默、紧张或释放改变处理资源和意义 | 情绪唤醒、注意分配、记忆选择；具体机制须经证据确认 | 情绪节拍真实、适量并服务于流程 | 情绪抢走效果；笑点时机暴露动作；高唤醒造成不可预测反应 |

每个误导节点必须包含：

- `mechanism_ids` 与边界化 `mechanism_summary`；
- `required_conditions`；
- `failure_modes`；
- `evidence_card_ids`，并区分科学证据和实践主张；
- `application_technique_ids`；
- `limitations` 与 `contradicting_evidence_card_ids`。

上表的“可能关联”只是本体允许的连接，不是已被批准的因果事实。只有证据卡审核通过后，具体的“机制解释误导原则”关系才可成为已批准知识。

### F. Performance Design：把魔术建模为剧场

表演设计由 `Technique` 节点表达，使用 `technique_family = performance_design`。它关注如何组织体验，不充当秘密方法。初始 `performance_facet` 词表为：

- `framing`：告诉观众应如何理解事件及赌注；
- `character`：表演者在作品中的身份、能力边界与行为一致性；
- `presentation`：效果的外在表达方式和观众参与方式；
- `script`：语言、停顿、问题和揭示信息的结构；
- `rhythm`：动作与信息的重复、对比和速度模式；
- `timing`：具体行动、台词、反应与揭示的时点；
- `dramaturgy`：冲突、升级、转折、高潮与余韵；
- `audience_relationship`：信任、距离、参与权、尊重和共同创造感。

每个表演设计节点至少包含：

| 字段 | 含义 |
|---|---|
| `performance_facet` | 上述受控分类 |
| `design_objective` | 希望改变的观众体验 |
| `audience_facing_choice` | 观众实际可见或可听的设计决定 |
| `sequence_scope` | `beat`、`phase`、`routine`、`show` |
| `required_conditions` | 角色、场地、互动和流程依赖 |
| `failure_modes` | 例如动机不清、节奏断裂、角色不一致 |
| `applicable_effect_ids` | 从 `uses` 关系派生 |
| `practitioner_provenance` | 记录提出、描述或示范该做法的获准来源，不声称科学有效性 |
| `evidence_card_ids` | 仅在存在独立可审核主张时使用 |

任务说明中列出的 *Framing Performance Magic*、Tommy Wonder、Juan Tamariz、Darwin Ortiz，只能在各自来源通过审核后作为 `Source` 或 `Performer` 的候选出处。其建议首先属于专家实践知识；除非另有科学证据，不得标记为实证心理规律。

### G. Expertise and Training：专业能力与训练

训练不是一个静态等级标签，而是技巧、前置条件、反馈和迁移的组合视图。本版本使用现有实体构造以下视图：

| 训练概念 | 表达方式 |
|---|---|
| Beginner skills | `Technique.minimum_proficiency = beginner` |
| Intermediate skills | `Technique.minimum_proficiency = intermediate`，并以 `requires` 指向前置技巧 |
| Expert cognition | `CognitiveMechanism.expertise_stage = expert`，科学主张必须有证据卡 |
| Practice methods | `Technique.technique_family = training_method` |
| Creativity | `Technique.technique_family = creativity` |
| Routine construction | `Technique.technique_family = routine_construction` |

训练技巧的附加字段为：

- `target_technique_ids`：训练针对的技能；
- `learning_objective`：可观察、可评估的目标；
- `practice_protocol`：练习步骤及顺序；
- `feedback_mode`：`self_observation`、`video`、`coach`、`audience_trial`、`instrumented`、`mixed`；
- `assessment_criteria`：准确度、自然度、稳定性、时机或迁移表现；
- `practice_context`：独练、排练、低风险观众、正式演出等；
- `progression_stage`：`beginner`、`intermediate`、`expert`、`all_levels`；
- `transfer_conditions`：从练习环境迁移到表演环境的条件；
- `common_plateaus` 与 `remediation_options`；
- `evidence_card_ids` 和 `practitioner_provenance`，二者必须分栏。

技能前置关系形成一个经过审核的有向无环图。若两个技能互相促进，应使用 `related_to` 或分别记录条件性训练主张，而不是制造循环 `requires`。

### H. Performer、Source、Research Paper 与历史视图

#### H.1 Performer

`Performer` 提供人物、角色和传统语境，建议字段包括：

- `canonical_name`、`aliases`；
- `roles`：表演者、作者、教师、创作者等；
- `active_period`：允许不确定时间范围；
- `traditions` 与 `performance_domains`；
- `associated_source_ids`；
- `attribution_status`：`documented`、`claimed`、`contested`、`unknown`。

`Effect performed_by Performer` 只表达有出处的表演实例或稳定关联，不代表创作权，也不代表该表演者认可所有变体。

#### H.2 Source 与 Research Paper

`Source` 是被访问、可定位和受权限约束的具体载体或版本；`ResearchPaper` 是学术作品的规范书目身份。一本书、访谈、讲义或网页通常只有 `Source`；一篇论文可以同时拥有：

- 一个 `ResearchPaper` 节点，保存题名、作者、年份、DOI 等作品级身份；
- 一个或多个 `Source` 节点，表示出版社页面、正式 PDF 或其他获准版本；
- `Source related_to ResearchPaper`，连接访问载体与作品。

来源节点至少保留 `source_type`、作者/创建者、发布日期、版本、稳定标识符、访问位置、权利状态、可引用定位符和审核状态。来源说明了主张来自哪里，不自动赋予主张可信度。

#### H.3 历史知识

历史不是单一“发明者”字段，而是可争议、可溯源的主张集合：

- `origin_period`：可为日期区间，不强迫伪精确年份；
- `geography_or_tradition`；
- `first_attested_source_id`：仅表示当前语料中的最早记载；
- `historical_status`：`first_attested`、`claimed_origin`、`adaptation`、`revival`、`independent_development`、`contested`、`unknown`；
- `inspired_by`：表达效果、方法或技巧的演化方向；
- 具体作者归属、首次发明和影响关系由证据卡承载。

不得把“当前找到的最早来源”写成“真实发明时间”。现有兼容别名 `created_by` 会归一为宽泛的 `related_to`；本体层不得借此绕过归属主张审核。

## 5. 科学机制、专家实践与个人解释的隔离

| 知识层 | 规范载体 | 可以声称什么 | 不可以自动声称什么 |
|---|---|---|---|
| 科学证据 | `ResearchPaper`、`CognitiveMechanism`、科学证据卡 | 某受控研究在特定条件下支持、限制或反驳某主张 | 研究结果必然适用于现场魔术 |
| 专家实践 | `Source`、`Performer`、`Technique`、`Method`、实践证据卡/主张卡 | 某专家描述、采用或建议某做法 | 该做法已被实验验证，或对所有观众有效 |
| 个人解释 | 待审主张或分析记录 | 某审核者/分析器提出一种可能解释 | 在获批前成为规范节点关系或高置信知识 |

关键规则：

1. `PsychologyPrinciple` 是应用概念，`CognitiveMechanism` 是科学解释构念；名称相似也不能合并。
2. 节点本身不持有一个全局“真值置信度”。置信度属于具体证据卡或具体关系断言。
3. `Source explains X` 表示来源讨论或记录 X，不等于来源科学地证明 X。
4. 从实验室任务迁移到现场魔术必须形成独立主张，显式记录生态效度限制。
5. 冲突证据与支持证据同为一等引用，不能只保留支持材料。

## 6. 规范关系、方向与约束

本体沿用当前六种 `RelationType`，不新增数据库专用边类型。

| 关系 | 允许方向 | 基数 | 语义与约束 |
|---|---|---|---|
| `uses` | `Effect -> Method/Technique/PsychologyPrinciple`；`Method -> Technique/PsychologyPrinciple`；`Technique -> PsychologyPrinciple` | 多对多 | 表示构造或应用依赖；不得仅由词语共现生成 |
| `inspired_by` | `Effect -> Effect`、`Method -> Method`、`Technique -> Technique` | 多对多 | 方向从后来的概念指向前项；禁止自环；历史影响必须有出处，不能把相似性当影响 |
| `requires` | `Effect/Method/Technique -> Technique`；`Method -> Method` | 多对多 | 表示必要前置；禁止自环；训练前置子图必须无环；一般适用条件留在属性中 |
| `explains` | `CognitiveMechanism -> PsychologyPrinciple/Technique/Effect`；`PsychologyPrinciple -> Technique/Effect`；`Source/ResearchPaper -> 任一领域概念` | 多对多 | 前两类是解释主张，需证据卡；来源到概念只表示内容覆盖或论述，不等于证明 |
| `performed_by` | `Effect -> Performer` | 多对多 | 只允许此方向；每条关联需要可定位出处，不表达创作权 |
| `related_to` | 任意规范实体之间 | 多对多 | 仅在没有更精确关系时使用；语义对称，存储时按 UUID 排序只保留一条，查询双向展开 |

关系级公共约束继承当前 `KnowledgeRelationship`：

- `source_id` 和 `target_id` 必须引用已有规范实体；当前 chunk 记录中，两个端点都必须存在于同一份 metadata；
- `type` 必须为上述六种值；
- `evidence` 只保存简短的关系断言或定位摘要，不保存整段受版权保护文本；
- `source_chunk_id` 在进入当前存储投影时必须可追溯；未来还应关联证据卡 ID；
- `confidence` 若使用，只评价这条断言，不继承为节点的全局置信度；
- 同一端点和关系类型可以有多份独立断言，但每份都需不同且可追溯的证据；读取时可合并展示，不得丢失分歧；
- 除 `related_to` 外，所有关系均按表中方向存储，不创建冗余反向边；
- GLM 提议的边在人工主张审核前只能是候选关系。

## 7. 贯穿示例：一枚硬币的消失

以下仅演示本体如何连接信息，所有名称、关系和解释均为“未审核示例”，不代表已批准知识或可入库内容。

### 7.1 体验层

```yaml
Effect:
  name: Illustrative Coin Vanish
  primary_effect_category: vanish
  audience_perception: 一枚可见硬币在一次自然转移后不再存在于预期位置
  emotional_impact: [surprise, wonder]
  apparent_sequence:
    - 硬币被展示
    - 硬币似乎转移到另一只手
    - 手被打开，硬币消失
```

这部分不提秘密动作，因此可以回答“观众经历了什么”，而不会把效果和方法混为一谈。

### 7.2 构造层

```text
Illustrative Coin Vanish (Effect)
  ├─ uses ─> Illustrative Secret Retention (Method: sleight)
  │            └─ uses ─> Illustrative False Transfer (Technique: execution)
  │                         └─ requires ─> Basic Concealment (Technique)
  ├─ uses ─> Assumption (PsychologyPrinciple: expectation)
  ├─ uses ─> Social Misdirection (PsychologyPrinciple: misdirection/social)
  ├─ uses ─> Motivated Gaze Cue (Technique: attention_management)
  ├─ uses ─> Reveal Timing (Technique: performance_design/timing)
  └─ performed_by ─> Example Performer (Performer)
```

`Method.visibility_risk`、`Technique.required_conditions` 和角度限制属于构造层。它们不改变观众层效果名称。

### 7.3 解释与证据层

```text
Illustrative Attentional Orienting (CognitiveMechanism)
  └─ explains ─> Social Misdirection (PsychologyPrinciple)

Approved Practitioner Source (Source)
  └─ explains ─> Illustrative False Transfer (Technique)

Approved Scholarly Work (ResearchPaper)
  └─ explains ─> Illustrative Attentional Orienting (CognitiveMechanism)
```

这里仍需三张可分别审核的主张：

1. 科学研究在其任务条件下支持某注意机制；
2. 该机制可以解释某种社会误导原则；
3. 该原则在这个硬币流程的具体条件下有应用价值。

第 1 项成立不自动推出第 2、3 项。实践来源可以支持“某做法被描述或使用”，不能单独证明注意机制。

### 7.4 表演与训练视图

- `Reveal Timing` 的 `design_objective` 是拉开秘密动作与效果确认的体验距离；是否有效仍取决于具体证据和上下文。
- `Illustrative False Transfer requires Basic Concealment` 让训练助手先检索前置技能。
- 一个 `training_method` 技巧可以 `related_to Illustrative False Transfer`，并记录视频反馈、动作自然度与稳定性指标。
- 若后来的变体有可审核的演化出处，可记录 `Later Handling inspired_by Earlier Handling`；若只有相似性，则不建影响关系。

同一个结构因此可以支持不同助手任务：解释观众体验、诊断暴露风险、提出表演设计检查项、生成有前置关系的练习路径，以及展示科学证据与实践建议之间的边界。

## 8. 图兼容性与当前存储边界

本体不要求现在部署图数据库。现有 `KnowledgeGraphRecord` 已能以数据库无关形式传递：

- `document_id`；
- `chunk_id`；
- 规范实体列表；
- 有向、可溯源的关系列表。

未来图适配器可以把八种规范实体投影为节点标签，把六种关系投影为边，并把误导、表演设计和训练画像作为额外标签或属性索引。规范 UUID、关系方向和证据引用不应因存储后端变化而改变。

在引入图适配器前，仍需完成：

1. 经人工审核的实体消歧、别名合并与拆分规则；
2. 关系断言的版本、冲突与撤销模型；
3. 来源权限和秘密暴露等级的端到端访问控制；
4. 证据卡与节点/关系之间的稳定引用；
5. 受控词表的变更审批与迁移规则。

当前阶段只批准本体结构，不创建任何实体实例，不写入 Qdrant，也不改变现有人工审批规则。

## 9. 面向未来 AI 魔术助手的能力视图

在数据经过审核后，本体应支持以下组合查询，而不是只返回相似文档：

- **体验解释**：从 `Effect` 到心理原则，再反向寻找解释它们的认知机制和证据卡；
- **方法比较**：比较实现同一效果的不同 `Method` 的技能、时机、观众条件和可见性风险；
- **失误诊断**：连接技巧常见错误、误导失败模式、表演设计和适用机制边界；
- **流程创作**：以效果体验为目标组合方法、技巧、节奏、脚本和情绪弧线，同时保留来源类型；
- **训练规划**：沿 `requires` 建立分级技能路径，并按反馈模式和迁移条件选择练习法；
- **历史追踪**：沿 `inspired_by` 查看有出处的演化链，并显式展示争议和未知；
- **证据审计**：分别显示科学证据、专家实践和个人解释，不把检索相似度当成知识置信度。

这些能力依赖后续证据、审核和提取设计；本文仅定义它们所需的认知结构。
