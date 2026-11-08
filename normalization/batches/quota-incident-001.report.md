# 额度回退故障与真实恢复

2026-11-08 19:02（UTC+8）检查时，实际成功文件共 **349 条**（旧 40 条 + full-001 的 309 条）。进程仍存在，但最近一小时没有新增成功，不能把进程存活称为正常推进。

## 原因与影响

Open Design 的实际错误事件为 `type: provider.quota`、`status: 402`，消息含 `[code=insufficient_balance] Coding Plan quota is exhausted`，并给出 `2026-11-08T11:52:16Z` 重置时间。原分类器没有识别该组合，将额度拒绝当作普通请求失败，没有启用已授权的 Go 回退。

19:02 请求滚动进程排空退出后，失败标记为 **8,461 条**。逐条检查最后一次保留事件，确认 **8,450 条是主模型额度拒绝**，另有 **11 条 JSON／Schema／引用错误**。这些额度拒绝没有产生成功记录，不计入转换数量；其缺失 token 用量不估为零，也不虚构账单费用。

## 修复与恢复

- 分类器补充实际 V2 的 `provider.quota`、`insufficient_balance` 与 `status` 字段，继续排除上下文及认证错误；解析真实重置时间。
- 主模型额度拒绝继续允许切到实时确认免费的 Go。Go 自身受限或非输入、非单请求超时的连接请求错误转 deferred，暂停领取；不再把服务故障批量标为材料失败。认证错误不启用新的模型。deferred 异常保留已发生请求的实际模型、会话、响应与用量。
- `--recover-quota-failures` 依据保留日志，仅恢复证明为主模型 quota 的失败。所有旧状态先压缩存档，原 attempt 文件不改，成功文件不动，真实格式失败保留。恢复为 deferred 的连接问题不再作为内容纠错反馈提供给模型。

实际恢复 **8,450 条、339 个批次**。完整原 state 备份在本机 `data/normalized/full-001/recoveries/quota-1794136275395153000/previous-states.jsonl.gz`，由 Git 排除。可公开的 [逐来源清单](../../data/normalized/full-001/recoveries/quota-1794136275395153000/requeued.jsonl) 与 [恢复报告](../../data/normalized/full-001/recoveries/quota-1794136275395153000/report.json) 保留原 attempt、状态哈希、依据与去向。恢复命令可重跑，已恢复或已成功来源不重复处理。

生产路由状态由这些**真实错误事件**恢复，未写入模拟额度状态。Open Design 返回的重置时刻为今晚 **19:52:16（UTC+8）**；届时按既有策略使用真实任务探测，不能在探测前宣称已恢复。

## 真实 Go 续跑

恢复前实时查询 `opencode api model.list`，确认 `opencode-go/iceman-contributor-free` active/enabled，input、output、cache read/write 均为 0。19:12 重启同一队列，保持 **10 路**，实查 10 个模型请求确实选择 Go，不以 metadata 中请求开始时的默认主模型标签推断实际模型。

截至 **19:15:45** 的已校验检查点：

| 口径 | 数量 |
| --- | ---: |
| 恢复后新保存的 Go 结果 | 12 |
| 累计成功 | 361 |
| 尚未成功 | 55,342 |
| 其中保留的内容／格式失败 | 11 |
| 其中待处理、在途及额度延期 | 55,331 |

计数依据真实文件和来源版本，目标仍为 55,703 个可读入口；历史另有 99 个未导出入口不计为已转换。8,450 是本次恢复的历史拒绝数量，随着 Go 成功处理，不代表当前仍有 8,450 条未完成。

该检查点 Go 有 **14 个返回会话**（含重试），已取得 input **163,433**、output **82,420**、reasoning **7,963**、cache read/write **0/0**。这些类别不机械相加；账单费用仍未知。全 full-001 的累计历史会话包含大量额度拒绝、用量不可取得，不能把会话数当作成功转换数。

检查点在 [report.json](../../data/normalized/full-001/checkpoints/20261108-quota-recovery-001/report.json)、[index.jsonl](../../data/normalized/full-001/checkpoints/20261108-quota-recovery-001/index.jsonl)、[remaining.jsonl](../../data/normalized/full-001/checkpoints/20261108-quota-recovery-001/remaining.jsonl)。record_path / detail_path 相对于所属检查点目录，指向本机持久保存的结果与状态。Git 不批量公开派生正文、完整请求或上游材料。

## 验证与继续

- `python3 normalization/run_tests.py`：原 87 项及 1,000 组区间 oracle 通过。
- `python3 -m unittest discover -s normalization -p 'test_*.py'`：46 项通过，新增实际 V2 错误回归、Go 自身受限后延期且保留用量、输入/超时继续按条处理、额度恢复证据/存档/幂等及成功复用检查。
- `python3 normalization/run_corpus.py --recover-quota-failures`：实际恢复 8,450 条，剩余原内容失败 11 条。逐条验证旧 state 存档与清单哈希一致，恢复前 349 条成功文件哈希未变。
- `python3 normalization/run_corpus.py --snapshot 20261108-quota-recovery-001`：361 条实际文件经原 Schema、来源映射及保留模型响应一致性检查，路径和哈希另行核对；Schema、检查器、转换约定和采集索引未修改。
- 抽查新 Go 输出中的 Windows 路径变体与 `O0000-code/paper-search-pro` 多阶段入口，对照固定原文和证据行号；抽查部分未发现需要暂停队列的共通含义错误。原文中的命令仅作为研究文本读取，未执行。所有结果仍为 `unreviewed` / `not_executed`。

核对记录见 [verification.json](../../data/normalized/full-001/quota-incident-001/verification.json)。后台继续运行，进程存在时无需重复启动；退出后从同一断点继续：

```bash
python3 -u normalization/run_corpus.py --workers 10
```

无需再次恢复已经恢复的历史条目，也无需重试全部格式失败。成功、失败、延期和剩余数量以之后的实际文件更新，不倒填本检查点。
