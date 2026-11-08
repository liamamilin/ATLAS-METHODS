# SkillRecord 标准化

格式固定为 **v0.1.1**，检查器为 **converged.1**。`batch_convert.py` 复用本机 OpenCode 中已连接的 Open Design，逐条生成、检查并保存真实模型转换结果；Schema 和检查器保持不变。

当前操作规则以 [CONVERSION-PROMPT.md](CONVERSION-PROMPT.md) 为准。下一项任务见 [NEXT-TASK.md](NEXT-TASK.md)。不叠加历史审查文件作为门禁，不重做 Schema。

第 001 批已实际处理 40 条，保存 40 条、失败 0 条、待处理 0。首次运行记录见 [第 001 批报告](batches/batch-001.report.md)，续跑及 Go 免费回退测试见 [模型切换报告](batches/provider-check-001.report.md)。历史人工样例和两份 Go 对照输出不额外计入这 40 个来源版本。

## 运行

Python 3.10+，复用已有虚拟环境。从仓库根目录执行：

```bash
python3 -m pip install -r normalization/requirements.txt
python3 normalization/run_tests.py
python3 normalization/export_response_schema.py > /tmp/atlas-response.schema.json
python3 normalization/check_record.py normalization/examples.v0.1.1.jsonl
```

`run_tests.py` 运行四组共 87 项已有回归及 1,000 次固定种子的区间覆盖对照。不要使用 Python `-O` 或 `PYTHONOPTIMIZE`。测试不联网、不调用模型、不执行上游脚本。新报告写在本目录并由 `.gitignore` 排除；过去的报告不冒充本次执行结果。

CLI 输入为一行一条记录的 UTF-8 JSONL，不是多行美化 JSON。退出码 0 仅表示该次结构与已检查的一致性没有错误；缺正文、章节未解析等提示不能被解释成原文已验证。单条错误不阻止后续记录检查。

## 模型与程序分工

模型输入为必要原文、真实仓库相对路径/行号、输出语言，以及导出的响应 Schema。模型只返回 `content`、`gaps`。`export_response_schema.py` 保留 `$defs`，不要手工剪掉定义；这是本地校验格式，不保证每家模型平台的受限 JSON 输出接口都直接兼容。

程序严格解析响应，然后合入：

- `schema_version`；
- 实际采集行映射的 `source`；
- 实际提供过的文件/范围、生成信息与状态 `extraction`。

再校验完整记录并保存。`id → collection_id`、`sha256 → entry_sha256`，其他来源字段原样复制；不能让模型猜来源或以一个相同正文的其他包代替当前来源。`gaps[].target` 使用完整记录内存在的 JSON Pointer；未生成的任务可以指向 `/content/tasks`。

## 接入已有语料

累计索引是 `data/cumulative/skills.jsonl` 或其无损 gzip 版本；可直接流式读取 gzip。累计记录中的 `raw_path`、`repository_manifest` 相对于索引目录。已有路径冲突映射必须沿用，不根据入口文件名猜正文位置。

原文不在当前环境时记录缺失，优先处理可访问材料。新结果放到 `data/normalized/`，不覆盖原文或累计索引。单条及时保存、已成功来源版本跳过、失败可重试；这些批处理能力由 `batch_convert.py` 提供。

## 实际批转换与续跑

沿用本机 OpenCode 的连接与认证，默认模型为 `open-design/deepseek-v4.1-flash`。无需将 API key 写进项目。先确认 `opencode` 在 PATH 中，或用 `--opencode /absolute/path/to/opencode` 指定。当前适配器针对实测的 OpenCode v2.2.2 `run` JSON 事件及 `session export` 格式。

```bash
python3 normalization/batch_convert.py \
  --selection normalization/batches/batch-001.selection.json \
  --out data/normalized/batch-001 --workers 4

# 同一命令继续未处理项；失败项需要显式重试
python3 normalization/batch_convert.py \
  --selection normalization/batches/batch-001.selection.json \
  --out data/normalized/batch-001 --workers 4 --retry-failed

python3 -m unittest discover -s normalization -p test_batch_convert.py
```

选择文件的 `items` 使用真实 `collection_id` 与固定 `commit_sha`；`references` 只列解决核心理解缺口所需的仓库路径。需要部分文件时可显式设置 `entry_ranges` 或参考项 `ranges`（LF 一基闭区间），不要默默截断。`--limit N` 仅限制本次新处理/重试项，不将其余项标为失败。默认每条最多两次普通请求/格式重试（可设 1—3）；额度切换允许至多一次额外请求，并保留两端尝试。每次调用有超时；单条失败继续下一条。

程序使用采集清单的实际 `raw_path` 读文件，发送前验证入口 SHA-256 和所有输入文件的 Git blob 哈希。`files_read` 仅记录实际嵌入请求的文件与范围，完整文本在内存中不代表全部送给模型。模型仅生成 `content/gaps`；来源、状态、时间和读取范围由程序合入，再调用原响应 Schema 和完整检查器。记录保持 `unreviewed` / `not_executed`。

输出包含：

- `records/<身份键>.json` 和汇总 `records.jsonl`：通过检查的实际转换；按 collection_id + commit 续跑跳过，不按正文哈希合并来源。
- `state.json`、`failures.jsonl`、`remaining.selection.json`：逐条进度、最后失败及剩余清单。成功标记缺少实际文件时不算完成；保存文件后中断可从文件恢复。输出目录带运行锁，避免两个批进程同时写入。
- `inputs/`、`attempts/*/*/metadata.json`：实际输入路径、范围、哈希、请求摘要、检查结果、模型会话 ID 和用量，包括失败请求。
- `report.json`：累计结果、当前调用的跳过/尝试数和可取得的用量。OpenCode 的 cost 只是其报告的估计；0 不证明免费，未核实账单费用保持未知。reasoning/cache 可能与 input/output 重叠，不机械相加。
- `attempts/` 中的完整请求、原响应、事件、会话和 stderr：持久保留本地并由 `.gitignore` 排除，避免把请求内上游原文批量公开。

每次转换启动独立临时 OpenCode Location，设置 cwd 与 PWD 一致，使用禁止全部工具操作的专用 primary agent，不加载本项目为编码任务。短请求的标准输入设为 DEVNULL；超过 120,000 字节的长请求通过单独受控管道传入完整提示词，避免命令行参数长度限制，不截断入口，也不继承父进程 heredoc。允许正常首轮回复，避免 steps=1 触发强制总结，工具仍全部禁止。固定 CLI 会话标题，不要求辅助模型生成标题。没有运行上游命令、脚本或 hooks；不会让模型自行取文件或补造阅读范围。权限字段依据 [OpenCode permissions](https://opencode.ai/docs/permissions/)，实际配置加载及 JSON/用量解析另有回归测试。

转换客户端通过 V2 `api config.get --standalone` 的配置来源列表，只提取 `mcp.servers` 名称；临时项目为这些名称写入 `disabled: true` 的完整惰性定义，不复制原 MCP 命令、环境或 provider 凭据。配置查询每个模型客户端缓存一次；原用户配置和模型连接不修改。覆盖形状遵循 [OpenCode MCP 配置](https://opencode.ai/docs/mcp-servers/#config)，真实 `mcp list` 与请求进程树均用于验证没有启动无用 MCP。配置形状未知时拒绝该次请求，不静默加载 MCP。请求 metadata 保留禁用名称。

格式提示补充 JSON 重复键、顶层 `content/gaps` 和 `gaps.target` 数字数组索引的正反例；已有双行号要求继续保留。模型仍生成完整响应，失败仍经原 Schema/检查器拒绝并有限重试；程序不重写模型正文、修造 JSON 或放宽校验。

## 全语料连续分批

`run_corpus.py` 使用现有逐条转换、来源检查、保存与汇总函数，以一个有界线程池滚动处理文件队列。冻结的 [full-001.queue.jsonl](batches/full-001.queue.jsonl) 包含剩余 55,663 个固定来源版本；已完成的 40 条从队列排除，实际结果文件可访问且结构核验通过后才复用。所有已保存原文共 55,703 条，历史 99 条未导出入口单独保留在 [unavailable.jsonl](batches/full-001.unavailable.jsonl)，不当作已转换。

从项目根目录启动或在进程退出后继续同一队列：

```bash
python3 -u normalization/run_corpus.py --workers 10
```

程序默认每批 25 条、4 个 worker；本机按用户要求显式 `--workers 10`，续跑时保留此参数。25 条的批次只作为目录与保存单位，10 个 worker 跨批次共享；完成一条即补一条，旧批次慢任务未结束时也可开始下一批，不改变已冻结批次的来源归属。不设置批次数上限、不逐批等待确认。已完成批次复用，失败记录后继续。额度导致模型不可用时，保存 deferred/pending，暂停领取更多材料，等待后再试，不把整库快速写成 deferred。

原批次目录、selection、state、records、attempts 与 report 继续沿用。首次交接会等待仍在运行的旧批转换子进程锁，复用其稍后写出的结果。新滚动进程收到 SIGTERM/SIGINT 后停止领取新任务，等待在途任务完成并保存，最后释放队列与批次锁；日常切换应等待这一过程退出后再续跑。成功结果缺失时不复用，保存成功文件而未更新 state 的中断可由原有恢复逻辑处理。

首轮结束后，对失败项再执行一次有限重试（成功项仍跳过）：

```bash
python3 -u normalization/run_corpus.py --workers 10 --retry-failed
```

新批次只从已保存清单解析入口直接引用的本地 Markdown/文本指南，不递归爬取整个包。选择至多四份、合计 40,000 字节的直接指南，优先显式提到的编译指南；未提供路径及原因写入冻结 selection 和模型输入备注，核心缺口由模型按现有约定简短说明。原入口保持完整；不声称整包覆盖或方法有效。

`data/normalized/full-001/batches/` 中的原响应、结果和详细错误均持久保留本机，不因处理完成就全部公开。来源许可未知不阻止本地处理；Git 只交付不含正文的来源索引、进度检查点及剩余清单。模型对照样例不算新增来源。

```bash
# 不调用模型，对当前已实际保存的结果生成不可变检查点
python3 normalization/run_corpus.py --snapshot YYYYMMDD-HHMMSS
```

检查点位于 `data/normalized/full-001/checkpoints/`。index 中 record_path、剩余项 detail_path 均相对于该检查点目录，指向真实本机文件；摘要核对原索引 source、记录结构和保留的模型响应。remaining 包含尚未成功的来源（包括失败、在途、未开始及额度延期）；快照数字带 as_of 时间，后台工作继续时不会假装它是实时值。用量只累计该次全语料运行的新请求，不重复计算复用 40 条的历史用量，未返回请求不虚构 token 或费用。

实时进度和日志在本机 `data/normalized/full-001/progress.json`、`runtime.json`、`worker.log`。滚动版 progress 的 `batches` 同时包含已结束与当前打开批次，计数包含当前批次已保存的成功/失败；`active_batches` 可以有多个，`scheduler_run` 只统计本次进程新领取任务、完成数、峰值在途数与槽位占用。检查点仍独立校验实际文件，不靠这些运行计数判定已转换。进程仍运行时不要重复启动；若退出，原命令续跑。这里只持续执行已授权的全队列任务，没有另建数据库、网站或定时服务。

## Open Design 优先与免费回退

默认加载 [model-routing.json](model-routing.json)，主模型为 `open-design/deepseek-v4.1-flash`，免费回退为实测的 `opencode-go/iceman-contributor-free`。原续跑命令即可使用：

1. 正常先用 Open Design。只有明确额度、429/速率或并发限制会转用回退；格式、认证、上下文长度等错误不会被当作额度耗尽。
2. 用 `opencode api model.list` 核验固定 Go ID 仍 active/enabled，所有输入、输出与缓存价格均为 0。价格未知、变为收费或模型不可用时保留为 deferred/pending，不换用其他付费模型。目录核验缓存最长五分钟。
3. 有额度重置时间时按其重试；未知时五分钟后，用下一条真实任务试探主模型，并发/速率限制通常三十秒后试探。跨线程/进程只允许一个恢复探测；成功后恢复主模型，仍受限则继续免费回退。已在途的旧主模型请求成功不算恢复证明。
4. 冷却与探测租约保存在本机 `data/normalized/.provider-routing-state.json`，跨批次和续跑保留；该运行状态及锁不提交。可用 `--routing-state` 指定独立状态文件，不能把模拟额度状态混入正式运行。

`--model` 显式选择其他模型时采用该单模型，不自动套用这份主模型策略。`extraction.producer` 和逐请求 metadata 记录实际使用的模型，报告分模型统计成功记录与用量。切换只在转换进程运行时或下次续跑时发生，没有后台轮询或定时服务；没有待处理任务就不额外花费请求来探测。

运行路由及批转换回归：

```bash
python3 -m unittest discover -s normalization -p 'test_*.py'
```

2026-11-08 实际遇到 V2 `provider.quota`、HTTP `status: 402`、`insufficient_balance` 组合，现已纳入额度分类并解析服务端重置时间。Go 自身受限或普通连接/认证请求失败时保存 deferred 并暂停领取，保留实际响应与用量，不将整库快速写成材料失败；认证失败不因此启用其他模型。明确的上下文输入错误和单请求超时继续按条有限重试。

`provider.content-filter` 是单条来源的拒绝，记录为 `failure_stage: content_filter` 后跳过，不触发全局等待、不换模型，也不重发这条来源；通用 `--retry-failed` 同样跳过它。旧版误归类为 deferred 的条目，续跑时只从来源匹配的保留事件纠正状态，原 attempt 不改，不为纠正分类再请求一次。实际修复与续跑检查点见 [过滤等待修复报告](batches/content-filter-incident-001.report.md)。

旧版本误记的额度失败已按保留事件恢复，见 [实际故障与恢复报告](batches/quota-incident-001.report.md)。仅在同类历史问题需要修正且队列进程已排空退出时运行：

```bash
python3 normalization/run_corpus.py --recover-quota-failures
python3 -u normalization/run_corpus.py --workers 10
```

恢复命令只处理保留日志能证明的主模型额度拒绝；不碰成功文件，不重新归类普通格式失败。完整旧 state 压缩备份保留本地，原 attempt 不改，Git 交付逐来源恢复清单。生产路由状态依据真实额度事件恢复，不混入测试额度标记；已恢复来源再次执行该命令不会重复恢复。可用模型连接故障不作为模型的内容修正反馈。

## 格式和校验边界

`source` 保留固定来源；`content` 包含任务、规则、资源、说明和证据；`extraction` 保留真实阅读范围与重要缺口。

`when: null` 不代表普遍适用；`tasks` 不是全局方法分类。允许正常空值、来源支持的概括、部分结果和章节引用，不用机械补空掩盖截断。`reviewed` 不作为保存和探索性比较门槛。

需要检查原文行界时，可通过 Python 调用：

```python
result = check_record(record, schema, source_texts=complete_fixed_commit_texts)
```

`source_texts` 为仓库相对路径到完整固定版本文本的映射，用来检查原始绝对行号。实际提供给模型的范围保存在 `files_read` 中：完整读取为 `full`；部分读取为 `partial` 并附真实 `ranges`。LF 一基闭区间，末尾 LF 不新增空行。检查器不独立证明模型实际阅读、来源身份或语义忠实度。

## 样例与可选来源复核

- `examples.v0.1.1.jsonl`：三条入口级示例。
- `pilot-records.jsonl`：内部沟通、代码审查的补读示例。
- `tool-backed-pilot.json`：GIF 工具型示例。
- `api-design-pilot.json`：接口设计示例。

合计七份记录变体、六个来源版本；内部沟通入口版与补读版共用来源身份，不能一起导入成两个独立 Skill。全部是此前人工整理的说明样例，仍标记未审阅/未执行，不计作本轮自动转换产量。

`source-audit.json` 仅提供旧两个补读样例所用七份文件的固定版本与摘要。已有对应原文时可运行：

```bash
python3 normalization/run_tests.py --source-root /path/to/pinned-snapshots
```

原文目录为 `<root>/<owner>/<repo>/<commit>/<原仓库路径>`；此功能不下载材料，不要求为转换复制整库，也不自动验证 GIF/API 示例。未提供目录就报告本次未重查原文。结构与引用正确不能代替内容判断或证明方法有效。
