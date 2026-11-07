# Go 免费模型测试与额度回退

日期：2026-11-07（Asia/Shanghai）。复用现有 OpenCode 连接与批转换器，SkillRecord v0.1.1、原检查器、目标优先转换文档和采集原文不变。

## 实测结果

- 默认主模型：`open-design/deepseek-v4.1-flash`。
- 免费回退：`opencode-go/iceman-contributor-free`。本机模型目录可见，实时 active/enabled，所有输入、输出、缓存价格为 0，见 [免费目录核验](../../data/normalized/provider-check-001/free-model.json)。[官方 Go 列表](https://opencode.ai/docs/console/go/)也将 Iceman Contributor Free 列为限时免费；没有把全部 Go 模型或订阅本身称为免费。
- Go 最小 JSON 测试最终通过；两条真实 SkillRecord 测试 **2/2 通过**，包含规则型品牌指南与补读两份关联 Skill 的 grill-with-docs。每条来源、输入范围、哈希及 content/gaps 与模型原响应一致。抽查了品牌色/字号/字体回退，以及设计树 frontier 前置条件和 ADR 三个条件必须同时成立。
- 这两条是对已成功来源版本的模型能力对照，**不计两个新增独立 Skill**。结果位于 [Go 对照记录](../../data/normalized/provider-check-001/skills/records.jsonl)，仍标记 unreviewed / not_executed。
- 使用默认策略实际续跑原首批：38 条成功结果完整复用，原 2 条失败现在由 **Open Design** 通过，正式首批累计 **40/40，失败 0、待处理 0**。没有为了测试回退强行耗尽真实额度。全库尚未启动转换。

## 客户端修复

最小测试暴露两个真实问题，前两次没有冒充 JSON 测试成功：

1. CLI 会将继承的标准输入追加给模型；在 Python heredoc 中调用时，父进程脚本被追加进请求。现将 run/export 的 stdin 设为 DEVNULL，并用真实子进程、带内容的父进程 stdin 做回归，避免污染实际输入范围。
2. agent 的 steps=1 会在首轮触发强制总结，Go 因此输出工作汇总而非指定 JSON。现用两步上限让首轮正常回复，全部工具权限继续 deny，并固定 CLI 会话标题。第三次最小 JSON 测试与后续两个完整转换通过。

原始事件、会话和响应保留本机 attempts/，由 .gitignore 排除；公开元数据只保留结果、摘要及可取得用量。没有运行上游脚本或让上游指令改变任务。

## 默认切换约定

[model-routing.json](../model-routing.json) 已默认接入 `batch_convert.py`，原批命令不需附加参数：

```bash
python3 normalization/batch_convert.py --selection normalization/batches/batch-001.selection.json --out data/normalized/batch-001 --workers 2 --retry-failed
```

1. 正常先请求 Open Design。明确 quota/额度、429 速率或并发限制才触发免费回退；认证、上下文长度、参数和 JSON/Schema 错误不作为额度用光处理。
2. 回退前通过现有 OpenCode 的 `api model.list` 核验固定 Go 免费 ID；active/enabled 且所有输入/输出/缓存价格必须明确为 0，目录核验缓存最长五分钟。不换用任何其他付费模型。不可用、收费或价格未知时保留为 deferred/pending，之后可续跑。
3. 有明确重置时间按该时间尝试，未知额度重置时间默认五分钟；并发/速率短限制默认三十秒。到期后仅让一个下一条真实任务探测 Open Design，成功即恢复主模型；仍受限则继续 Go。不会用一条在额度失败前已在途的成功请求错误地宣布恢复。
4. 本机 `data/normalized/.provider-routing-state.json` 持久保存冷却、探测租约和切换事件，跨批次/进程恢复；状态、锁和临时文件不提交。崩溃后的探测租约会过期，不会永久卡住。
5. 每条至多一次额外额度切换请求，普通失败仍按 --max-attempts 有限重试。每次请求单独保存，不覆盖主模型失败响应；producer、metadata 和 report 的分模型用量反映真正使用的模型。

切换在转换进程运行或下一次续跑时执行。空闲且没有任务时不会额外发请求，也没有后台轮询、定时服务或充值行为。`--model` 显式选其他模型时是单模型测试，不自动套用主模型策略。

本次主模型实际可用，运行状态没有真实的额度回退事件。额度耗尽→Go→恢复探测→切回的完整周期通过 **10 项模拟提供方错误的回归测试**，不把模拟事件写进正式额度状态或声称是实际耗尽后恢复。

## 本轮可取得用量

只列本轮新增请求，不把上一轮的模型消费重新算成本轮。原始逐会话返回详见 [usage.json](../../data/normalized/provider-check-001/usage.json) 和各 attempt metadata。

| 请求组 | 有用量的主模型会话 | input | output | reasoning |
|---|---:|---:|---:|---:|
| Open Design 补齐两条 | 2 | 12,571 | 7,123 | 3,307 |
| Go 两条 Skill 对照 | 2 | 11,062 | 6,629 | 1,380 |
| Go 三次最小测试（含两次客户端问题） | 3 | 1,807 | 720 | 247 |
| 合计可取得 | 7 | 25,440 | 14,472 | 4,934 |

reasoning/cache 可能包含在 input/output 中，不能机械相加。CLI 对这些请求返回 cost=0；Open Design 账单费用未知，不用该估计声称免费。Go 的具体 ID 当前目录价格明确为 0；限时免费未来可能变化，因此运行时持续核验，不以历史测试授权收费回退。没有申请或购买新服务。

## 验证与保存

```bash
python3 normalization/run_tests.py
python3 -m unittest discover -s normalization -p 'test_*.py'
```

既有 **87 项回归及 1,000 次区间对照**通过。相关 unittest **27 项**通过（含既有 9 项响应契约、8 项批转换、10 项额度路由），覆盖额度识别、普通错误不误切、恢复单次探测、旧在途请求、已知重置时间、跨实例租约恢复、价格变更拦截、实际模型/用量及原始双端尝试保存。

对正式 40 条及 Go 对照 2 条复用原检查器，核验真实索引身份、Git blob / SHA-256、实际提供范围与模型响应。原 38 条记录逐字段不变。两组分别重跑，新增模型请求和 attempt 文件均为 0，记录字节不变。见 [核验结果](../../data/normalized/provider-check-001/verification.json)。

正式材料仍在 `data/normalized/batch-001/`，对照与测试在本机持久目录 `/Users/milin/Documents/ChatGPT/ATLAS-METHODS/data/normalized/provider-check-001/`。Git 只提交必要代码、已核验的派生记录、用量/来源元数据及报告；完整提示词、原响应、会话、凭证、运行锁及上游快照不批量上传。来源许可/署名沿用 [首批线索](batch-001.NOTICES.md)。
