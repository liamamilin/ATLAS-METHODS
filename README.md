# ATLAS-METHODS

从 GitHub 上真实的 Skills 出发，自下而上整理可理解、可比较、可回查的具体做法，再逐渐归纳 Method 与 Domain。**不预先用领域树给材料填空。**

## 当前重点

采集阶段已有固定版本语料。下一步是用大模型把已有材料批量转换为 **SkillRecord v0.1.1**，不再继续设计 Schema。

- 已接入：Schema、目标优先转换提示词、响应 Schema 导出工具、检查器、测试和人工样例。
- 待完成：接入实际模型调用、可续跑的批转换程序，并产出真实转换批次。
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

从仓库根目录执行，建议使用现有项目虚拟环境：

```bash
python3 -m pip install -r normalization/requirements.txt
python3 normalization/run_tests.py
python3 normalization/export_response_schema.py > /tmp/atlas-response.schema.json
python3 normalization/check_record.py normalization/examples.v0.1.1.jsonl
```

这些命令验证格式和工具，不会调用模型、转换全库或执行上游 Skill。真正的批转换命令由下一项开发任务提供。

## 采集成果保持不变

原有采集器、合并器、批次记录与原始材料不迁移、不覆盖。累计索引仍使用 [data/cumulative/skills.jsonl.gz](data/cumulative/skills.jsonl.gz)，本机可继续保留完整 JSONL。

本次接入之前的完整 README 已原样保存在 [采集指南](docs/collection-guide.md)，其中包含采集命令、数据路径、压缩索引恢复及历史统计。最新采集报告见 [docs/collection-run-018.md](docs/collection-run-018.md)。

标准化记录另存 `data/normalized/`。核心判断是有没有保留具体做法和重要限制、能否回到来源，而不是字段是否填满或每条是否经过深审。
