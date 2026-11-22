# 待归类目录提案

这个运行器只调用本机已登录的 OpenCode CLI，为待归类条目生成目录分类提案。它不写 `catalog.sqlite3`，不写分类覆盖层，也不把提案当成源文审阅。

模型固定为 `opencode-go/iceman-contributor-free`。每次运行先读 `opencode api model.list`，确认该模型仍是 active、enabled，且输入、输出、缓存读写价格都是 0。实际会话导出的 provider、model 和 cost 也必须匹配；cost 不是 0，或价格不再是 0，就停止后续请求，不改用别的模型。

全局同时在途最多 10 条。第二个进程看到锁会直接退出。已完成且指纹未变的条目不会再请求。模型输出若不是合法 JSON、领域不是既有 id、专题不是该领域下的既有名称，或证据不是本次输入摘要/任务里的连续原文，就保留原始输出并记为错误，不会被收成通过的提案。

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

持续跑使用 `--mode continuous`，仍然最多 10 路在途，并且必须使用另一个 `--out`。不要和 10 条试跑共用一个目录。
