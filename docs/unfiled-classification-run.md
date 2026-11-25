# 待归类目录提案

这个运行器只调用本机已登录的 OpenCode CLI，为待归类条目生成目录分类提案。它不写 `catalog.sqlite3`，不写分类覆盖层，也不把提案当成源文审阅。

模型固定为 `opencode-go/iceman-contributor-free`。每次运行先读 `opencode api model.list`，确认该模型仍是 active、enabled，且输入、输出、缓存读写价格都是 0。费用只认每个 assistant 回合自己的 `cost` 字段。会话级 `info.cost`、最后一个成功回合的 0，以及 user、agent-switched、synthetic、idle 上的默认 0，都不能补上前面错误回合缺失的费用。默认 `--cost-policy strict`：缺 `cost`、非 0、身份不可核、模型不一致或 tool 事件都会停止后续新请求。`--cost-policy allow-missing-cost` 只在这次显式授权下使用：模型仍是 `opencode-go/iceman-contributor-free`，四项报价仍必须是 0，身份必须核得上，tools 必须是 0。这时 assistant 回合缺 `cost` 记为 `missing_cost_allowed_by_user`，不写成 0，也不因此停掉整批；内容、引用或结构错误仍拒绝该条。明确非 0、报价不再为 0、身份偏离或 tool 事件仍然全局停止，不能换模型。已经因 `cost_unknown` 停住的目录，要加 `--resume-missing-cost-halt` 才会继续新候选，并只把“仅仅缺费用”且内容合格的已保存结果收成提案；其他停止原因和旧账本不清除，也不会自动重跑旧失败。汇总里的 `fee_proven_all` 只有全部已核回合都是 0 时才为真。

响应契约是 `catalogue-classification-response/v2`。`decision` 只能是 `propose_existing_category` 或 `retain_unfiled`。`evidence` 必须是数组；每一项包含 `field`（`summary` 或 `task`）、`quote` 和 `task_index`。专题名必须与现有词表逐字相同，包括空格。旧契约目录不会被改写成成功，原始 `attempts/` 留在原处，重试使用新的 `--out`。

全局同时在途最多 10 条。第二个进程看到锁会直接退出。已完成且指纹未变的条目不会再请求。模型输出若不是合法 JSON、领域不是既有 id、专题不是该领域下的既有名称，或证据不是本次输入摘要/任务里的连续原文，就保留原始输出并记为错误，不会被收成通过的提案。出现 tool 事件的那一条记为错误，并且不再调度新的模型请求。

调用放在空的临时目录里，并写入只在该目录生效的 `atlas-catalogue` 代理。权限按 OpenCode v2 的 `permissions` 规则拒绝 `read`、`glob`、`grep`、`edit`、`shell` 以及其他工具。不修改用户的全局配置，不复制认证，也不在临时目录放 `AGENTS.md`。

在 Mac 上先做 10 条试跑。游标和排除文件以当时目录为准；下面的游标是 2026-11-22 的起点，执行前仍要核对下一条身份。

```bash
cd /Users/milin/Documents/ChatGPT/ATLAS-METHODS
python3 library/classify_unfiled.py \
  --opencode /Users/milin/.opencode/bin/opencode \
  --mode trial \
  --after 1373f8e8768d27651b5c4b3ff528b1f0 \
  --exclude /绝对路径/all-454-review-proposals.json \
  --out data/library/classification-runs/trial-001
```

试跑结果在 `--out` 里：`proposals.json` 是尚未导入的提案，`attempts/` 保留请求、原始事件和回执，`report.json` 有调用数、在途峰值和停止原因。确认报告后再用现有导入命令做只读预览，不要在这个运行器里直接写入覆盖层。

契约修正后的 10 条复测使用新目录，不要把上一轮的 `retain_unfiled` 放进 `--exclude`。暂保留不是终审，也不是永久排除。检查入口只对照摘要和任务里的既有用词，不根据标题给新归类，也不写目录：

```bash
python3 library/classify_unfiled.py \
  --review-retains data/library/classification-runs/trial-001/proposals.json \
  --out data/library/classification-runs/trial-001-retain-review
```

持续跑必须同时给出三个上限，并且使用另一个 `--out`。`--max-candidates` 最大 100，到上限后停下来做现有的单次预览和导入。在途仍然最多 10，不是每批再开 10 路。

```bash
python3 library/classify_unfiled.py \
  --opencode /Users/milin/.opencode/bin/opencode \
  --mode continuous \
  --after 1373f8e8768d27651b5c4b3ff528b1f0 \
  --max-candidates 100 \
  --max-requests 200 \
  --max-runtime-seconds 3600 \
  --out data/library/classification-runs/batch-002
```
