# 全队列批转换：full-001

已启动持续分批处理；本报告是进度检查点，不代表全部转换完成。统计截至 **2026-11-08 15:21:14（UTC+8）**，后台任务仍继续运行。

| 口径 | 数量 |
| --- | ---: |
| 可访问的已保存 Skill 入口版本 | 55,703 |
| 复用的既有成功记录 | 40 |
| 本次冻结的剩余队列 | 55,663 |
| 本轮新增成功 | 43 |
| 累计成功 | 83 |
| 尚未成功 | 55,620 |
| 其中已记录失败 | 2 |
| 其中在途、未开始或额度延期 | 55,618 |

成功按 `collection_id + commit_sha` 计数，要求实际结果文件存在。旧的 40 条不再调用模型；免费模型对照实验的重复来源不计入新增。历史另有 **99 条未导出入口**（96 个符号链接、3 个非 UTF-8/二进制材料），已保留在 [unavailable 清单](full-001.unavailable.jsonl)，不计入本次可读队列，也不宣称已转换。

## 队列与执行

本轮没有重新发现来源或刷新上游版本。由累计采集索引冻结 [55,663 条来源版本](full-001.queue.jsonl)，按仓库轮转排序，不做领域、名称、正文或语义筛选。队列 SHA256 为 `40c76fcfc2471e9cc7941427e5b818501f827d2482e3a42e726ba0fb2c65360e`；生成时累计索引 SHA256 为 `73fbc79fb79e9b1119470b863c230d8bce82edfe2826e7dceefa0424525b115b`。

`run_corpus.py` 只在原有 `batch_convert.py` 外循环：每批 25 条、4 个 worker，无批数上限。逐条保存结果，已完成批次复用；失败保留详情并继续下一批。不可用模型导致的 deferred 保持待处理，等待后续尝试。文件锁防止同队列重复启动；父进程重启时等待存活的批转换子进程。

首批 25 条已终结（24 成功、1 失败），第二批自动开始。队列父进程曾完成一次断点交接：新父进程 PID `67378` 在 15:08 启动，首批所有模型请求早于此次启动，首批报告仍为 15:06 生成，没有重复转换该批。PID 与当前批次以本机 runtime / progress 为准。progress 的成功与失败计数只累计**已结束批次**；本报告检查点还计入当前批次已实际保存的成功结果，因此两者更新时点不同。

模型路由继续使用既有策略：优先 `open-design/deepseek-v4.1-flash`；额度、限流或并发限制时，仅回退到运行时确认启用且所有计价为零的 `opencode-go/iceman-contributor-free`。冷却或额度恢复后由真实任务探测 Open Design，成功后恢复优先使用。此检查点所有实际返回的调用均为 Open Design，尚未发生本轮真实额度耗尽与恢复，不能把离线回退测试称为已发生的线上切换。

入口全文保持完整。只从实际清单选择入口直接引用的本地 Markdown/文本指南，至多四份、合计 40,000 字节，不递归扩展整包；未提供的路径和原因写入 selection 及模型输入备注，重要缺口由模型记录。针对语料中实际存在的超长入口，将超过 120,000 字节的完整请求改为受控 stdin 管道，避免命令行长度限制，不截断材料；短请求仍使用 DEVNULL 防止继承父进程输入。受控长输入经过子进程测试，本检查点不声称已对所有长入口完成线上转换。

## 失败与复核

截至检查点有两条终态失败，均已保留两次尝试的请求、响应、用量和错误；没有手工修补模型内容或放宽检查：

| 来源 | 入口 | 最后一次原因 |
| --- | --- | --- |
| `agiwhitelist/auteur` | `SKILL.md` | 模型 JSON 不完整，解析报 `Expecting ',' delimiter` |
| `AugustusW/audio-tldr-skill` | `skills/audio-tldr/SKILL.md` | 部分证据仅有 `line_start`，缺少必填的 `line_end`，原检查器拒绝 |

抽查了普通竞争分析入口、Aeon 多模式入口和 MaxCompute 运行时分发存根的输出及原文。抽查部分的目标、步骤与约束可对应原文；存根没有被补造为完整运行时工作流，缺失指南有 gaps。该抽查未发现需要暂停全队列的共通问题，不代表全条目语义审阅；结果仍保持 `unreviewed`，不宣称方法有效。

## 模型用量

仅统计本次 full-001 新请求，不重复计算复用 40 条的历史用量：

| 模型 | 已有会话用量记录 | input | output | reasoning | cache read / write |
| --- | ---: | ---: | ---: | ---: | ---: |
| `open-design/deepseek-v4.1-flash` | 51 | 663,079 | 385,155 | 757,846 | 0 / 0 |

失败和重试产生的已返回用量也计入；检查点另有 4 个未结束 attempt，其尚未返回的 token 未虚构。reasoning 等字段可能与输出重叠，不机械相加。未取得账单费用，`billing_cost` 为未知，CLI 的零估计不作为免费证明。

## 保存位置与继续运行

新增正文、完整模型请求/响应和错误持久保存在本机项目目录的 `data/normalized/full-001/batches/<批号>/records/`、`attempts/` 和 `state.json`。原采集材料及旧批次保持原位。未知许可不阻塞本地整理，也不自动授权公开分发；新批次正文不批量推送，Git 交付来源、哈希、相对本机路径和进度。

可公开检查点为 [report.json](../../data/normalized/full-001/checkpoints/20261108-001/report.json)、[index.jsonl](../../data/normalized/full-001/checkpoints/20261108-001/index.jsonl)、[remaining.jsonl](../../data/normalized/full-001/checkpoints/20261108-001/remaining.jsonl) 和 [verification.json](../../data/normalized/full-001/checkpoints/20261108-001/verification.json)。`record_path` 和 `detail_path` 均相对于**所属检查点目录**，在当前本机可访问；仅克隆 Git 不会获得这些本地正文。首次中间检查点也保留用于追溯，本报告数字采用 20261108-001。

当前进程仍运行，不要并发启动。进程退出后，从项目根目录续跑：

```bash
python3 -u normalization/run_corpus.py --workers 10
```

首轮结束后对失败项再作一次有限重试，成功项继续复用：

```bash
python3 -u normalization/run_corpus.py --workers 10 --retry-failed
```

生成新时间点的来源/进度检查点（不调用模型，使用新的目录名）：

```bash
python3 normalization/run_corpus.py --snapshot YYYYMMDD-HHMMSS
```

## 已执行验证

- `python3 normalization/run_tests.py`：87 项检查及 1,000 组区间 oracle 通过。
- `python3 -m unittest discover -s normalization -p 'test_*.py'`：35 项通过，覆盖原批转换与路由、长请求受控 stdin、新队列断点、文件锁、失败保留、来源映射及保存后尚未更新 state 的恢复。
- `python3 normalization/run_corpus.py --snapshot 20261108-001`：83 条实际记录通过现有结构检查，内容与保留模型响应一致，source 与真实采集索引一致。
- 检查点逐条核对：83 条路径可访问且 SHA256 一致；55,620 条 remaining 无重复，与成功集合不交叉，两集合合计 55,703。
- `git diff --exit-code -- normalization/skill-record.v0.1.1.schema.json normalization/check_record.py normalization/export_response_schema.py normalization/CONVERSION-PROMPT.md data/cumulative/skills.jsonl.gz data/normalized/batch-001`：通过，Schema、转换约定、原检查器、采集索引及旧 40 条均未改动。

全队列仍在进行；最终完成、失败重试和未来用量应依据后续实际结果更新，不将本报告快照当作整库完成。

## 15:43 并发调整

按用户要求，使用既有参数 `--workers 10` 续跑，保持每批 25 条，不修改转换器、Schema 或模型策略。仅停止旧队列父进程 `67378`，保留其在途批次与模型请求；新父进程 `93624` 在 15:43:06 启动，等待第三批子进程释放文件锁后接续。已经完成的来源继续复用，未手工修改或放宽失败结果。此处操作记录不改变上方 15:21 检查点的统计口径。

15:43:59 已自动接续第四批；15:44 实查批次子进程 `94379` 下有 **10 个直接 OpenCode 请求进程**，同时存在 10 个未结束 attempt，确认实际并发已生效。当时路由状态没有额度/并发限制事件，仍使用 Open Design；15:45:25 第四批已实际保存 3 条成功结果，尚无失败或 deferred。并发增大后的有效吞吐尚需后续批次测量，不将并发倍数当作已实现的速度倍数。续跑命令已同步保留 `--workers 10`。
