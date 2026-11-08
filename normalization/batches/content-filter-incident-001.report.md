# 单条过滤误触发全局等待：检查与恢复

2026-11-08 **21:26（UTC+8）**实查，已实际保存 **895 条**来源版本，最近一小时新增 245 条。后台进程与任务锁仍存在，但当时为 `waiting_for_model`、在途 0，不能称为正在进行 10 个请求。

## 原因与最小修复

Go 对一条来源返回 `provider.content-filter`，消息为 `Provider blocked the response`。此前通用服务故障保护把这个带连字符的拒绝类型归为全局模型不可用，导致整队列进入等待。

现已将该类型明确归为**单条拒绝**：保留源材料及原响应，状态记为 `failed / content_filter`，然后跳过。不会改写输入、换模型或再次发送被拒绝条目，通用 `--retry-failed` 也不会重发它。未来同类新拒绝直接记录一次后继续其他来源。额度、连接与认证故障原有延期保护保持；Schema、原检查器、转换约定及模型路由配置未改。

受影响来源为 `yaklang/hack-skills` 的 `skills/active-directory-acl-abuse/SKILL.md`，commit 为 `6fbf0bc8d5c71830d62543a308d8744606c43d7c`。续跑从来源匹配的旧事件纠正 deferred 状态，没有为纠正状态而新增请求。原 `attempts/36e5bcb929e69372c683652b670da4d5/004` 的 metadata 与事件哈希未变，也没有更晚的 attempt。之前的延期错误保留在 state 的 `previous_availability_error`，原日志继续留存本机。

## 实际恢复结果

旧进程按 SIGTERM 排空，排空期间在途结果继续保存；到停止时有 **917 条**成功文件，全部哈希记入 [切换前清单](../../data/normalized/full-001/content-filter-incident-001/before.json)。21:33 以同一命令恢复，仍为 10 路滚动补位、Open Design 优先与确认免费的 Go 回退，未将 Luna 加入批路由。

截至 **21:35:53** 的已校验检查点：

| 口径 | 数量 |
| --- | ---: |
| 累计成功 | 929 |
| 此次重启后新成功 | 12 |
| 尚未成功 | 54,774 |
| 其中其他失败 | 23 |
| 其中 provider 过滤跳过 | 1 |
| 其中待处理、在途或延期 | 54,750 |

23 条其他失败和 1 条过滤跳过合计为机器报告的 `failed: 24`，都包含在剩余数量中。目标仍为 55,703 个实际可读入口；历史 99 个未导出入口不计为已转换。旧的 917 条成功文件未改动，当前新成功均有实际文件，不能用进程存活、来源数或请求数替代产量。

此时原队列已正常领取其他来源，实查在途 10，没有继续请求被过滤来源。程序继续后台运行；检查点是固定时间数据，不倒填之后的完成数量。

## 验证与交付

- `python3 normalization/run_tests.py`：87 项与 1,000 组区间 oracle 通过。
- `python3 -m unittest discover -s normalization -p 'test_*.py'`：49 项通过，新增实际连字符事件分类、单条拒绝不重试且其他条目继续、旧 deferred 凭保留事件修正且不新请求的测试。
- `python3 -m py_compile normalization/model_routing.py normalization/batch_convert.py` 与 `git diff --check` 通过。
- 实际断点交接：917 个旧成功文件哈希一致，旧拒绝事件／metadata 原样保留，被拒绝来源没有新增 attempt；新进程产生成功结果，峰值并发为 10。
- `python3 normalization/run_corpus.py --snapshot 20261108-content-filter-001`：929 个实际文件通过原结构检查、真实采集来源映射和模型原响应一致性检查；逐条核对路径和哈希，成功与剩余身份不交叉。

校验数据见 [verification.json](../../data/normalized/full-001/content-filter-incident-001/verification.json)。公开检查点包含 [report.json](../../data/normalized/full-001/checkpoints/20261108-content-filter-001/report.json)、[index.jsonl](../../data/normalized/full-001/checkpoints/20261108-content-filter-001/index.jsonl)、[remaining.jsonl](../../data/normalized/full-001/checkpoints/20261108-content-filter-001/remaining.jsonl)。索引路径相对于检查点目录，正文和全部详细尝试留在本机持久目录，不批量公开。

当前进程运行时无需重复启动。退出后按原命令继续：

```bash
python3 -u normalization/run_corpus.py --workers 10
```
