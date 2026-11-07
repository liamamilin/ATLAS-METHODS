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

每次转换启动独立临时 OpenCode Location，设置 cwd 与 PWD 一致，使用禁止全部工具操作的专用 primary agent，不加载本项目为编码任务。标准输入设为 DEVNULL，避免 OpenCode 将父进程 heredoc 追加给模型；允许正常首轮回复，避免 steps=1 触发强制总结，工具仍全部禁止。固定 CLI 会话标题，不要求辅助模型生成标题。没有运行上游命令、脚本或 hooks；不会让模型自行取文件或补造阅读范围。权限字段依据 [OpenCode permissions](https://opencode.ai/docs/permissions/)，实际配置加载及 JSON/用量解析另有回归测试。

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
