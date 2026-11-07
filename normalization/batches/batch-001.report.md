# Open Design 批转换第 001 批

> 本报告记录提交 `6fa204e` 时首次运行的 38/40 快照。后续续跑已补齐为 40/40，并加入 Go 免费回退；见 [后续实测报告](provider-check-001.report.md)。下方链接的运行文件会随续跑更新，首次快照可在该历史提交中回查。

日期：2026-11-07（Asia/Shanghai）。从 main `a8e9636` 快进拉取后执行。沿用 SkillRecord **v0.1.1**、converged.1 检查器、目标优先转换约定及原采集数据，没有修改 Schema、检查器、响应 Schema 导出器或 CONVERSION-PROMPT.md，没有重新采集上游。

## 实际结果

冻结清单共 **40 个入口 / 版本**，来自 **7 个仓库**。实际由 Open Design 生成并保存 **38 条**通过检查的记录，**2 条失败，0 条本批待处理**。这是第一次实际模型批转换；历史七份人工样例及修订前的存档不算新增产量，同一来源版本不重复计数。

原采集库仍有 55,703 条已保存入口；其中 **55,663 条未选入本批**。加上本批失败 2 条，共 55,665 条尚无本轮通过检查的正式模型记录。这与本批 pending=0 是不同口径，不宣称全库标准化完成。

- [固定选择清单](batch-001.selection.json)、[实际记录](../../data/normalized/batch-001/records.jsonl)、[状态及用量](../../data/normalized/batch-001/report.json)。
- [失败](../../data/normalized/batch-001/failures.jsonl)、[剩余清单](../../data/normalized/batch-001/remaining.selection.json)、[逐项状态](../../data/normalized/batch-001/state.json)。
- [来源/范围与续跑核验](../../data/normalized/batch-001/verification.json)、[语义抽查](../../data/normalized/batch-001/semantic-review.json)、[许可及署名线索](batch-001.NOTICES.md)。

记录 source 来自实际采集行：id → collection_id，sha256 → entry_sha256，仓库、commit、Git 路径及 blob 原样复制。正文按清单 raw_path 读取，发送前核对原始字节的 SHA-256 / Git blob；不从 skill_path 猜本机路径。模型只返回 content/gaps，程序合入元数据，保持 unreviewed / not_executed。

本批选择覆盖不同来源、规则型入口、短路由入口、复杂条件、工具型材料和指南主导的材料。只补读明确需要的资料：内部沟通四份指南、grill-with-docs 的两个 Skill、gws-shared、React 组合模式完整编译文档、十份主题文本以及 MCP 的主要指南。没有递归读完所有脚本和可选引用。

成功记录实际提供了 **61 个文件读取项，涉及 57 个不同的仓库+commit+文件**。每条 files_read 只表示真正嵌入请求的范围；选定 40 条共准备并验证 63 个文件项，不能将请求模型前失败的准备工作当作转换成功。

## 几条可读结果

这些是实际模型输出的标题和概括；各条仍为未完整审阅，描述来源做法，不表示执行过或已证明有效。

- **内部沟通写作：按类型选择指南并套用固定格式**（`anthropics/skills`，5 个实际提供文件）：该 Skill 是一套内部沟通写作资源：先从请求判断沟通类型（3P 更新、公司通讯、FAQ 等），再从 examples/ 载入对应指南，按其中的格式、语气与信息采集要求撰写；没有匹配指南时向用户澄清格式。3P 更新强调 30–60 秒可读、每节 1–3 句、数据驱动与严格固定版式；公司通讯强调 20–25 条要点、使用“we”人称、多链接并按板块归类；FAQ 强调面向全公司找共性困惑并基于官方信息作答。
- **拷问式方案打磨并在过程中同步产出 ADR 与术语表**（`mattpocock/skills`，3 个实际提供文件）：该 Skill 本身只是入口：它要求调用 Skill 工具两次，分别执行 grilling（按设计树分轮拷问用户，直到达成共识）和 domain-modeling（在设计过程中主动构建并锐化领域模型，就地写 GLOSSARY.md，并在满足三个条件时少量提议 ADR）。组合后的用途是：在把方案或设计问透的同时，把澄清后的术语与关键决策沉淀为文档。
- **React 组合模式（Composition Patterns）**（`vercel-labs/agent-skills`，2 个实际提供文件）：面向 React 代码库的组合式组件做法：用显式变体组件、compound components 与 context/provider 取代以布尔 props 定制行为的单一巨型组件，并把状态管理上提到 provider、以 state/actions/meta 的通用 context 接口做依赖注入。入口按四类规则给出优先级，具体解释与错误/正确代码示例分布在各规则文件与编译文档 AGENTS.md 中；React 19 API 变更单列，仅适用 React 19+。
- **构建高质量 MCP 服务器：研究、实现、测试与评估流程**（`anthropics/skills`，4 个实际提供文件）：该指南给出创建 MCP（Model Context Protocol）服务器的四阶段流程：先研究 MCP 规范与目标 API、规划工具集；再搭建项目、实现共享基础设施与带 schema/注解的工具；随后做代码审查、构建与 Inspector 测试；最后编写 10 道只读、独立、复杂、答案稳定的评估题并自行验证。文档强调服务器质量以“能否让 LLM 完成真实任务”衡量，并给出命名、分页、响应格式、传输、安全与错误处理等具体约定。

## 实际问题与处理

首轮 7 条在请求模型前失败，共 14 个 OpenCode 会话：子进程 cwd 已改变，但继承的 PWD 仍指向项目，OpenCode v2.2.2 因而没有加载临时转换 agent，返回 Agent not found。修复为 cwd/PWD 同时指向独立临时 Location；专用 primary agent 禁止所有工具，复用 OpenCode 原有 Open Design 连接与认证。对应回归验证了真实子进程的目录、配置、事件和导出解析。

校准有 6 条通过、1 条结构失败后继续剩余队列。扩大阶段共 36 条通过、4 条失败；抽查再发现 brainstorming 摘要把 spike 说成唯一例外，遗漏了用户主动 opt-out。修订前记录和原响应完整保留，给模型重新提供固定原文并指出原文第 38–40、276–279 行，模型的新摘要明确保留两个例外，已回查。

模型曾输出重复 JSON 键、嵌套在 content 内的 gaps、缺少 line_end、越界行号及不存在的 gap target，均由原响应 Schema / 检查器拒绝。有限重试保留每一次原响应，不剪掉约束或编造字段。后续请求明确给出源文件 LF 行数与真实范围，并提醒重要缺口、复用任务引用和合并重复动作；Schema 及检查器保持不变。

另有一次 provider.invalid-request，返回 tier_limit_exceeded / membership concurrency limit exceeded。该请求未返回 token 记录，保留为用量未知；其他成功记录没有丢失。最终重试并发降为 2。

最终仍失败的两条：

- `mattpocock/skills:skills/engineering/code-review/SKILL.md`（collection_id `e36d5d8a160259015b36`）：`['content']: Additional properties are not allowed ('gaps' was unexpected); []: 'gaps' is a required property`。原响应保留在本机 `data/normalized/batch-001/attempts/c0145739dd89ea0be92aa06bbaa2fe9c/003/response.txt`，没有补造字段或计为成功。
- `mattpocock/skills:skills/engineering/triage/SKILL.md`（collection_id `5416b11c81efc2f9f021`）：`['content', 'tasks', 0]: 'checks' is a required property; ['content', 'tasks', 0]: 'evidence' is a required property`。原响应保留在本机 `data/normalized/batch-001/attempts/3a5e358c8577cb9f70c5ba7231ed7c99/003/response.txt`，没有补造字段或计为成功。

抽查覆盖 **8 条**，优先看上述新形态、复杂条件、参考资料主导和工具型材料，另查品牌规则及 Web 测试普通材料。保留了少量重复描述、非必要缺口和章节定位提示；没有因措辞或字段归属反复重写。全批仍保持 unreviewed；不把这 8 条的针对性抽查称为逐条完整审阅。核验中有 **5 种章节定位提示**，完整内容见 verification.json；不影响已检查的结构、来源和行范围结果，也未声称所有语义引用已独立核实。

## 可取得的用量

正式转换尝试目录共 **75** 次记录 / OpenCode 会话：14 次 agent 解析失败发生在模型前，**60 次返回模型 token 用量**，1 次额度拒绝未返回用量。这里包括格式失败、重试及语义修订，不仅统计最终 38 条。

| OpenCode 返回字段 | 转换及重试 | 连接校验（不计产量） |
|---|---:|---:|
| input | 456,155 | 708 |
| output | 260,171 | 2 |
| reasoning | 182,975 | 163 |
| cache read / write | 0 / 0 | 0 / 0 |

连通性诊断的一次 OK 请求另有 [连接校验记录](../../data/normalized/batch-001/connection-check.json)，没有冒充 Skill。把有返回的连接校验也算上，可取得 input **456,863**、output **260,173**、reasoning **183,138**；reasoning/cache 与 input/output 可能重叠，不能机械相加成总量。一次未认证 HTTP 探针返回 401，没有模型用量返回，不计作成功推理。

OpenCode 对这些会话报告 cost=0；**账单费用未知**，未核实服务定价、订阅扣量或实际账单，不以 0 声称免费。用量来自逐会话 session export，完整明细在各 attempt 的 metadata.json；不使用账号全局统计冒充本批用量。

## 保存与复现

持久目录为本机 `/Users/milin/Documents/ChatGPT/ATLAS-METHODS/data/normalized/batch-001/`。

- records/ 及 records.jsonl 是当前 38 条正式结果，inputs/ 保留实际范围与哈希。
- attempts/ 逐次保留 request.txt、response.txt、events.jsonl、session.json、stderr.txt，原响应未改写；这些含完整研究材料的文件由 .gitignore 排除，仅保留本机。修订前的派生记录另存 accepted-before-review.json。
- Git 提交代码、冻结清单、可公开派生记录、输入与用量元数据及报告；没有提交凭证、缓存或整个上游快照。第三方许可文本与入口声明单独保留，不统一改为本项目许可。

本轮已实际执行的模型命令包括：

```bash
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --limit 7 --workers 2
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --limit 7 --workers 2 --retry-failed
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --workers 4 --retry-failed
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --workers 2 --retry-failed --max-attempts 1
```

下次只继续失败项，复用已成功 38 条：

```bash
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --workers 2 --retry-failed
```

不加 --retry-failed 时只处理尚未开始项，跳过已记录失败；同一个 collection_id + commit 不重复计数。新来源另建选择清单和输出目录，复用同一程序。程序读取 gzip 累计索引，不需要重采、解压重提交超限索引或安装上游 Skill。

## 验证结果

```bash
python3 normalization/run_tests.py
python3 -m unittest discover -s normalization -p test_batch_convert.py
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --workers 2
```

- 既有 **87 项回归 + 1,000 次区间对照**通过；新增 **8 项批转换回归**通过，覆盖逐条失败、格式重试、相同正文不同来源、原始路径映射、真实范围、原文字节改动、续跑/中断恢复、语义反馈和 OpenCode 子进程适配。
- 对最终 38 条复用原 check_record 与响应 Schema，传入完整固定来源文本，结构及已检查的一致性通过。40 个选定入口及所选资料重新计算 Git blob；每条来源字段与实际索引一致，读取范围与输入清单一致。
- 最终 content/gaps 与保留的模型响应逐项相等；每条请求摘要与本机 request.txt 相同，没有用模板或规则拼接代替模型。
- 最后一次重跑跳过 **38 个成功、2 个失败**，新增模型调用 **0**，attempt 数量不变，records.jsonl 字节完全相同。该命令退出码 **1** 是两条已有失败仍在报告内，符合预期，没有将其抹掉或报为成功。
- 首次运行的 records.jsonl SHA-256：`abfce3e000f9016580583f9fd4de3d718f03117bb7763c15596d0d3923670549`。采集索引、原文、Schema、原检查器均未改动；未执行研究材料中的命令或脚本。
