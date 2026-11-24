# 单条作品：怎样把分析写到能照着改

> 2026-11-23当前成稿：保留下文四步、方法和完整分析职责，新报告用[连贯正文规范](report-prose-layout.md)及`report_prose`自然分页为4～5页。下文图解v2、短卡片与`report_view`交接仅用于历史兼容，不作为新任务默认；新正文不截取碎片、不强制一页一章。


用于“分析自己的作品”。保留定目的、看数据、拆原因、给改法四步。以下完整分析作为底稿；新报告按[连贯正文规范](report-prose-layout.md)填写report_prose，自然分页4～5页。证据、解释与行动在正文讲清，完整字段不全部打印。

新准备任务默认连贯正文：作品平台与角色沿事实包传入video，主页核验单独保存，首页显示；按中文成稿规范用完整段落解释主要问题及改法。外部稿保留实际来源，不覆盖空API原始答复。旧图解v2只兼容历史稿。

## 使用顺序

1. 用 `scripts/review_analysis.py prepare` 把已有素材回执、账号背景和数据来源整理成事实包。没有新媒体回执也可整理现有事实后使用同一契约；未取到原片时不能声称完成画面分析。不要重新下载已有视频。
2. 成稿模型读取事实包、review-methods.json、本页及连贯正文规范，查看实际帧图，填写analysis_detail后写report_prose。按证据选方法，解释主要判断与优先动作；不要把推理只留底稿，让正文剩下结论。
3. 执行 `review_analysis.py validate <report_data.json>`。错误先修；警告交给内容复核判断，不为通过检查硬补字数或堆术语。随后仍由 `build_report_pdf.py` 输出 PDF 与 Markdown。
4. 看完逐页渲染结果，再核对“为什么改”是否真的落实在台词和拍法中。通过结构校验不等于内容已有效，也不等于人工听校完成。

## 四步各自必须多交出什么

| 步骤 | 读者需要得到的答案 | 避免的薄稿 |
|---|---|---|
| 定目的 | 谁在什么处境下想知道什么；作品实际完成什么；和已知账号定位有何关系；成功应看什么 | 只贴“了解型/成交型”标签；看见家长就自动改成招生 |
| 看数据 | 具体值说明什么；不能证明什么；应该回原片查什么；主要目标有没有直接结果 | 只有数值和空参考；用低播放推出选题失败；把评论数当需求正文 |
| 拆原因 | 原片如何推进；哪些做法值得保留；哪些环节缺了信息或证据；人物和画面怎样服务理解 | 用一个时间戳支持一句泛评；从静帧判断全片无动效；把作者口述当效果证明 |
| 给改法 | 先改哪一处；具体怎么说、拍什么、素材是否已备；如何接下一段；发布后怎样判断 | “优化开头/加强互动/丰富画面”；结尾替用户加领资料、咨询、免费服务 |

## 从来源到方法

`review-methods.json` 是本商品公开方法说明对应的执行问题，不是固定效果公式。`K01` 先核受众与任务；`K02` 选场景/误区/案例/方法骨架；`K03` 核开头与兑现；`K04` 核推进；`K05` 核主张与可见证据；`K06` 核真实评论；`K07` 核迁移条件；`K08` 核数据解释。每次只为实际发现选择相关方法，不逐项引用凑篇幅。

截图里的“观众由好奇变心动”等是该示例的分析解释。新报告可解释一句话希望解决什么理解障碍，不能写成已测量的心理或平台权重。平台指标只提示查哪里，原片证据支持内容判断，两者结合也不自动构成因果证明。

## 结构化交接：analysis_detail

新详细模式在原 `creator/video/metrics` 旁增加 `analysis_detail`。生成器提供完整空结构。以下为字段职责；`fact_packet` 指向同目录 `fact_packet.json`，只存内部证据，不随正文逐项打印技术编号。

### purpose：目的与账号匹配

- `audience`、`situation`、`viewer_question`：用具体的人、任务和疑问表达，不能只有“目标人群”。
- `primary_task`：曝光/了解/成交；`goal` 是具体目标，`success_evidence` 写能支持达成的实际结果。
- `goal_status`：confirmed/context_assumption/unknown。context_assumption 必须写明借用哪份已有背景及适用限制，不能冒充该视频主页已核。
- `evidence_refs`：指向事实包的账号、视频等证据。
- `account_fit`：`helps` 写作品为人物增加了什么识别度；`mismatch` 写哪里可能偏离定位；`decision` 写保留什么及如何收拢受众。没有主页简介时写无法逐字核对，不让这项阻塞脚本诊断。
- `overall_judgment`：先说主要优点与主要缺口，别重复所有数据。

### data_reading：数据怎样支持判断

- `rows[]`：挑会影响决定的重点指标，不逐个数字硬拆一章。`metric_key` 必须在事实包存在；`observation` 是读数；`meaning` 是与目的相关的解释；`inspect_next` 是接下来回原片查什么。只有某项特别容易被误读时才填可选 `not_proven`，共通限制在数据范围说一次。原始数值由脚本读取，不让模型重抄。
- `goal_result`：先回答主要目标，缺归属信息或结果明细时不能用关注、私信猜成交。
- `comments.status`：missing/available；missing 时只说明无法从正文归纳需求，仍分析选题给出的可能需求。available 时 `evidence_refs` 必须指向实际评论，`sample_scope` 交代样本，`conclusion` 与 `content_action` 接到实际原文。
- 无比较数据时不渲染一整排“参考—/差距—”。历史快照、按标题匹配、是否属于当前作品，在资料说明处一次讲清。

### timeline：原片按功能分段

每段有 `start_s/end_s`、`content`、`task`、`evidence_refs`、`handoff`、`decision`、`reason`。按实际转折分段，不机械均分秒数。`handoff` 写与下一段的逻辑连接；末段写如何收束。decision 取 keep/shorten/move/replace/drop。

先还原整条片，再选重点。第一帧不等于实际开场段，开头提问不等于全片从提问开始；要核0秒至第一个话题转换。

### findings：有证据的保留与调整

每条使用唯一 `id`，`dimension` 取 persona/script/visual；`decision` 取 keep/improve。

`title → evidence_refs → observation → explanation → action → verify → method_refs` 缺一不可：

- observation 说这条视频在何处实际出现什么；机器稿用释义，不把未核的词改成引号原话。
- explanation 说明这一段原本负责什么、目标观众已得到什么、还缺哪一步。不要重复 observation。
- action 写保留哪段、怎样改顺序、补哪句、配哪种证据或怎样拍。仅写“增强代入感”不算动作。
- verify 写怎样检查建议落实及下次观察什么。不能用“没有咨询就证明开头不好”这种单一归因。
- method_refs 指向 K01—K08 的适用方法；来源只支持检查办法，不证明该建议已经有效。

选择一个清楚的主问题，通常展开一个保留决定和一至两处优先调整。人物、脚本、画面都要检查，但不为填满维度制造三个缺点。其他观察可收在时间线与画面表里。

### coverage 与 visual_audit：防漏看，不强迫每项都有问题

`coverage` 对 opening/audience/progression/proof/sharing/transfer/comments 逐项填写：`status` 为 analyzed/missing/not_applicable，`finding_refs` 指发现编号；缺失/不适用写 `reason`。评论正文缺失可记 missing，不捏造认可或质疑。

`visual_audit[]` 覆盖 composition/person/lighting/background/props/subtitles/decoration/motion。每项写 `status` observed/unknown、`evidence_refs`、`observation`、`impact`、`action`。

画面构图要写人物位置与可见景别；字幕写位置、层级和遮挡；灯光只写可见明暗与面部辨识，不从照片推灯具；背景和道具写它们帮忙解释什么。动效与节奏没看连续片段就记 unknown，不从静帧得出“没有动效”。建议只在有必要时增加装饰/动效，并说明它帮助读懂哪一项信息。画面只在这里集中展开，不另拆两个画面章节。

### plan：让每条改法都能落地

- `priority`：一句话说明先改什么以及为什么先做；不能把所有发现都列成第一优先。
- `actions[]`：`finding_refs`、`instruction`、`needed_material`、`material_status` ready/to_prepare/not_needed、`fallback`。待备材料要有不虚构结果的替代方案。
- `structure[]`：建议段落的 `name/task/content/shot`，以及 `finding_refs`。它是下一条的骨架，不是擅自扩写整篇仿拍稿。凡结构承诺的环节，都应有文字和画面建议。
- `replacements[]`：`finding_refs`、`source_refs`、`label`、`suggested_text`、`visual_instruction`、`reason`、`transition`。给开头、关键正文、结尾的局部示范，不能只改开头而缺正文证据。建议句不混入未经证实的履历、效果或服务。

写“前五秒出现某对象”时，对象要在建议开头第一句出现，试讲后再核实际时长，不能只在理由里承诺。建议新增的人工核对等动作，写成建议/待确认，不悄悄说成系统当前已运行的固定步骤；无素材版示意图也遵守这一点。

`suggested_text` 必须能由创作者对观众自然说出；“实际流程还要核实再讲”“等用户提供素材后填写”等制作指令放在 `visual_instruction` 或行动准备中，不混入口播。材料不足时，直接提供明确的示意讲法，或保留可说的已知部分；不要让创作者在镜头前读模型的待办事项。交付前把建议台词单独连读一遍检查。
- `transfer`：`skeleton`、`variables`、`conditions`、`failure_risks`。提炼可复用结构及哪些东西要换，不把一次做法写成万能爆款公式。
- `follow_up`：`window`、`watch`、`interpretation`、`next_decision`。同平台、相近任务与窗口才作比较；没有可比作品时，先记下这次结果，供下一条比较。观看、主页访问和实际交流分别说明，不把它们拼成同一批人的行动过程。
- `invitation`：报告最后邀请用户提供自己的下一条脚本，再按提炼的节奏给文字与拍摄建议，不自动发片或添加服务。

## 校验的边界

脚本检查证据和方法编号能否找到、数值是否来自事实包、时间是否越界、各重点建议是否对应发现、结构是否遗漏文字或拍法、评论和动效是否越过资料范围、附件是否真实存在。它会拒绝空字段及“未分析却直接出报告”的产物。

字段填齐仍可能空泛。内容复核必须实际判断：解释是否回答“为什么”；动作是否改变了所指的问题；证据是否足以支持表述范围；新句是否像本人；建议是否擅改目标或添了承诺。不得写“通过校验，所以已证明普通模型都能高质量执行”。普通模型前向试跑只证明被测模型、这组输入和该次执行。
