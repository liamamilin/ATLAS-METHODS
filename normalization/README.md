# SkillRecord 标准化

格式固定为 **v0.1.1**，检查器为 **converged.1**。本目录提供标准格式、转换约定、校验工具和样例；**尚未实现实际模型调用与可续跑批转换程序**。

当前操作规则以 [CONVERSION-PROMPT.md](CONVERSION-PROMPT.md) 为准。下一项任务见 [NEXT-TASK.md](NEXT-TASK.md)。不叠加历史审查文件作为门禁，不重做 Schema。

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

原文不在当前环境时记录缺失，优先处理可访问材料。新结果放到 `data/normalized/`，不覆盖原文或累计索引。单条及时保存、已成功来源版本跳过、失败可重试；这些批处理能力仍需按 NEXT-TASK 实现。

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
