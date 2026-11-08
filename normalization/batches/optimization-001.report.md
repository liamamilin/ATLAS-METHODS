# 10 路批转换效率优化实测

保持 SkillRecord v0.1.1、原响应 Schema、检查器、转换约定与模型路由不变，已完成跨批次滚动补位、临时禁用 MCP、格式提示增强，并真实处理新材料。全队列继续在本机运行，不把这次验证称为整库完成。

## 实际速度

从 **2026-11-08 16:06:01 到 16:19:37（UTC+8）**，连续保存前 **50 条**通过检查的新来源版本，用时 **13 分 36 秒，约 220.6 条／小时**。同一验证过程的前 25 条用时 8 分 27 秒，约 177.5 条／小时。

| 实测范围 | 成功数 | 耗时 | 成功数／小时 | 请求槽位占用估计 |
| --- | ---: | ---: | ---: | ---: |
| 优化前完整第 4 批，10 路 | 25 | 9 分 45 秒 | 154.0 | 61.3% |
| 优化后前 25 条 | 25 | 8 分 27 秒 | 177.5 | 99.9% |
| 优化后前 50 条 | 50 | 13 分 36 秒 | 220.6 | 99.9% |

50 条窗口的样本吞吐比前述优化前批次高约 **43%**。两组是不同的连续新材料，输入长度、输出长度及重试分布不同，**不是同输入 A/B**，不能把差值全部归因于代码或承诺后续一直保持该速度。25 条和 50 条是重叠观察窗口，不能相加成 75 条。槽位占用按请求墙钟时间估算，不是模型 CPU 利用率。

原始测量元数据在 [measure-025.json](../../data/normalized/full-001/optimization-001/measure-025.json)、[measure-050.json](../../data/normalized/full-001/optimization-001/measure-050.json)，包含窗口起止、失败、在途尝试、用量和旧批次基线。没有重复转换旧的成功材料来制造对照数据。

## 三项改动

1. **跨批次滚动补位。** 原每批 25 条的目录、selection、state、records、attempts 和 report 全部沿用；批次只作为保存单位。一个上限为 10 的线程池共享这些批次，完成一条即补位。复用 `batch_convert.py` 中的输入检查、逐条转换、成功恢复、结果写入和汇总，不另建数据库或调度平台。第 7 批首请求在 **16:12:28** 开始，第 6 批直到 **16:15:07** 才结束，实际观察到跨批重叠。峰值在途数为 10。
2. **仅关闭转换进程中的无用 MCP。** 每模型客户端只读查询一次 OpenCode V2 配置来源，仅提取 MCP 名称，在临时项目中写入完整的禁用覆盖。没有复制全局命令、环境或 provider 凭据。真实 `opencode mcp list --standalone` 返回 `No MCP servers configured`；任务进程树中的 Godot／Blender MCP 从此前观察到的共 20 个降为 **0**。原用户 `opencode.json` 和 `opencode.jsonc` 哈希保持不变。配置规则见 [OpenCode MCP 配置](https://opencode.ai/docs/mcp-servers/#config)。
3. **针对实际格式错误增强提示。** 明确禁止重复 JSON 键，强调 `content/gaps` 顶层位置，给出 `gaps.target` 数字数组索引的正反例，并保留证据同时提供 `line_start/line_end` 的要求。继续由模型生成完整响应，保留原错误反馈和有限重试。没有重写模型内容、自动改造错误 JSON、修改 Schema 或放宽检查。本次仍有结构／引用错误，样本不能证明格式错误率已经降低。

新滚动进程收到 SIGTERM/SIGINT 时，停止领取新任务，等待在途转换保存后退出再释放锁。重启继续复用成功文件；失败仅在显式 `--retry-failed` 时重试。额度无法使用时暂停继续领取，保留 deferred，等待后再试。Open Design 优先、仅确认免费后使用 Go、恢复后切回的既有策略不变；本验证窗口实际均使用 Open Design，未触发线上额度切换。

## 实际结果、失败与保存

切换时有 **158 条**可复用的成功记录（原 40 条加此前 full-001 的 118 条）。[切换前清单](../../data/normalized/full-001/optimization-001/before.json) 保留了这些真实文件的哈希；后续核对全部未改动。

截至 **16:20:07** 的已校验检查点：

| 口径 | 数量 |
| --- | ---: |
| 优化后新增已保存成功 | 53 |
| 累计成功 | 211 |
| 尚未成功 | 55,492 |
| 其中历史及本轮终态失败 | 8 |
| 其中在途、未开始或额度延期 | 55,484 |

首个新生产批次第 6 批为 **24 成功、1 失败**，第 7 批为 **25 成功、0 失败**；后续批次已经滚动开始。原语料目标仍为 55,703 个已保存入口版本，另有历史 99 个未导出入口单列，不计为已转换。

50 条验证窗口中共启动 66 个 attempt，其中 6 个为额外重试；窗口结束时 57 个已返回，9 个尚在途。6 次重试由 2 次 OpenCode 超时和 4 次结构／引用错误触发。唯一终态失败为：

- 仓库：`first-fluke/oh-my-agent`
- commit：`73a27ed52cfb892d0727c34109d55a59b42e0869`
- 入口：`.agents/skills/oma-academic-writing/SKILL.md`
- 最后原因：证据中使用了不允许的 `end_line` 字段。原检查器拒绝，原请求／响应／错误保留在本机，随后来源继续运行。

新增正文和全部详细响应继续保存在本机持久目录 `data/normalized/full-001/batches/`，原采集材料保持原位。Git 仅提交不含正文的来源、哈希、路径和实测报告。检查点 [index.jsonl](../../data/normalized/full-001/checkpoints/20261108-optimization-001/index.jsonl) 的 `record_path` 及 [remaining.jsonl](../../data/normalized/full-001/checkpoints/20261108-optimization-001/remaining.jsonl) 的 `detail_path` 均相对于所属检查点目录，指向实际本机文件。

## 模型用量

以下仅为 50 条窗口截止前**已返回且取得用量**的请求，包含失败与重试，不重复计入复用记录的历史用量：

| 模型 | input | output | reasoning | cache read / write |
| --- | ---: | ---: | ---: | ---: |
| `open-design/deepseek-v4.1-flash` | 797,509 | 425,816 | 749,320 | 0 / 0 |

57 个返回会话中有 **2 个未取得 token 用量**，另有 9 个在途 attempt 尚未返回；上述 token 是可取得的部分，不能当作全部实际消耗。reasoning 等字段可能与 output 重叠，不机械相加。账单费用未知。全 full-001 的累计用量另见 [检查点 report.json](../../data/normalized/full-001/checkpoints/20261108-optimization-001/report.json)，其统计范围与本窗口不同。

## 验证与继续

- `python3 normalization/run_tests.py`：87 项及 1,000 组区间 oracle 通过。
- `python3 -m unittest discover -s normalization -p 'test_*.py'`：42 项通过，新增覆盖跨批补位、并发上限、失败后继续与显式重试、排空后恢复、旧批锁交接、MCP 配置隔离／缓存及格式错误反馈。
- `python3 -m py_compile normalization/batch_convert.py normalization/run_corpus.py` 与 `git diff --check` 通过。
- 真实 MCP 列表与任务进程树确认未启动无用 MCP；全部已结束新 attempt 的 metadata 保留禁用名称。
- `python3 normalization/run_corpus.py --snapshot 20261108-optimization-001`：检查点中的 211 条实际记录经原结构检查，内容与保留模型响应一致，source 对应真实采集索引；并逐条核对记录路径与 SHA256。
- 旧 158 条文件哈希和全局配置哈希未变。Schema、原检查器、响应 Schema 导出器、CONVERSION-PROMPT、采集索引、旧 40 条与路由文件的 Git 差异均为空。
- 抽查 `evalstate/fast-agent` 简短入口和 `Ericyoung-183/alpha-insights` 多阶段入口，对照固定原文与证据行号；抽查部分未发现需要暂停队列的共通含义错误。结果仍为 `unreviewed` / `not_executed`，不声称整包覆盖或方法有效。

详情见 [verification.json](../../data/normalized/full-001/optimization-001/verification.json)。进程仍在运行时无需再启动；退出后续跑命令保持：

```bash
python3 -u normalization/run_corpus.py --workers 10
```

首轮结束后仅重试失败项：

```bash
python3 -u normalization/run_corpus.py --workers 10 --retry-failed
```

进度以本机 progress、runtime 和 worker.log 为准；此报告与公开检查点固定在各自时间，不把后台后来完成的数量倒填到旧窗口。
