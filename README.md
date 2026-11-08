# ATLAS-METHODS

从 GitHub 上真实的 Skills 出发，自下而上整理可理解、可比较、可回查的具体做法，再逐渐归纳 Method 与 Domain。**不预先用领域树给材料填空。**

## 当前重点

采集阶段已有固定版本语料。目前使用 Open Design 把已有材料批量转换为 **SkillRecord v0.1.1**，不再继续设计 Schema。

- 已接入：Schema、目标优先提示词、检查器，以及复用已有 OpenCode/Open Design 连接的可续跑批转换器。
- 真实模型批次：[第 001 批状态与用量](data/normalized/batch-001/report.json)、[转换记录](data/normalized/batch-001/records.jsonl)；逐条保存，成功跳过，失败显式重试。
- 2026-11-07 首批实际处理 7 个仓库的 40 条入口：保存 40 条，失败 0 条，待处理 0。默认 Open Design 优先，额度限制时回退到实测 Go 免费模型、恢复后切回；[切换策略及实测报告](normalization/batches/provider-check-001.report.md)。这些记录仍为未完整审阅、未执行。
- 2026-11-08 已启动剩余 55,663 条的全队列连续分批转换，复用原 40 条；[本轮检查点与运行报告](normalization/batches/full-001.report.md)。新结果和完整响应留在本机持久目录，公开索引保留来源、哈希和可取回路径。
- 10 路转换已接入跨批次滚动补位、临时禁用 MCP 和格式提示优化，沿用原批次与断点；[优化实测报告](normalization/batches/optimization-001.report.md)。
- 已修复实际额度错误漏识别，恢复 8,450 条额度拒绝并真实切换至 Go 免费模型继续；[故障与恢复报告](normalization/batches/quota-incident-001.report.md)。
- 已按要求清理约 6.29GB 的采集 Git 缓存，原文、清单、索引及转换结果保留，未重启转换进程；[清理与保留校验记录](docs/cache-cleanup-20261108.json)。后续重新采集会按来源清单的固定 commit 重新下载缓存，现有转换直接读取保存好的原文。
- 人工样例是格式和内容参考，不是自动转换结果或方法有效性的证明。

编码 Agent 先读 [AGENTS.md](AGENTS.md)，然后执行 [批转换任务](normalization/NEXT-TASK.md)。

## 标准化入口

| 文件 | 用途 |
|---|---|
| [normalization/README.md](normalization/README.md) | 运行方式、数据接入及当前能力边界 |
| [CONVERSION-PROMPT.md](normalization/CONVERSION-PROMPT.md) | 最新目标优先约定，替代此前多轮审查要求 |
| [skill-record.v0.1.1.schema.json](normalization/skill-record.v0.1.1.schema.json) | 完整记录格式 |
| [export_response_schema.py](normalization/export_response_schema.py) | 派生仅含 content/gaps 的模型响应结构 |
| [check_record.py](normalization/check_record.py) | 结构及轻量一致性检查 |
| [batch_convert.py](normalization/batch_convert.py) | 复用已有模型连接，批转换、逐条保存、续跑和失败重试 |

从仓库根目录执行，建议使用现有项目虚拟环境：

```bash
python3 -m pip install -r normalization/requirements.txt
python3 normalization/run_tests.py
python3 normalization/export_response_schema.py > /tmp/atlas-response.schema.json
python3 normalization/check_record.py normalization/examples.v0.1.1.jsonl
```

上述命令仅验证格式和工具。以下命令实际调用本机 OpenCode 已连接的 Open Design；同一输出目录重跑时跳过已保存的成功来源版本：

```bash
python3 normalization/batch_convert.py \
  --selection normalization/batches/batch-001.selection.json \
  --out data/normalized/batch-001 --workers 2 --retry-failed
```

完整请求、原响应和会话留在本机，Git 只保存可公开派生记录、输入范围、用量元数据及报告；不执行上游 Skill。

全队列进程退出后的续跑命令（进程仍运行时无需重复启动）：

```bash
python3 -u normalization/run_corpus.py --workers 10
```

## 采集成果保持不变

原有采集器、合并器、批次记录与原始材料不迁移、不覆盖。累计索引仍使用 [data/cumulative/skills.jsonl.gz](data/cumulative/skills.jsonl.gz)，本机可继续保留完整 JSONL。

本次接入之前的完整 README 已原样保存在 [采集指南](docs/collection-guide.md)，其中包含采集命令、数据路径、压缩索引恢复及历史统计。最新采集报告见 [docs/collection-run-018.md](docs/collection-run-018.md)。

标准化记录另存 `data/normalized/`。核心判断是有没有保留具体做法和重要限制、能否回到来源，而不是字段是否填满或每条是否经过深审。
