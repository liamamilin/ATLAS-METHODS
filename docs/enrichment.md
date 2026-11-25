# 分层标签第一增量

这层注释写在目录旁边的 sidecar 里，契约是 `atlas-skill-enrichment/v0.1`。它不写入分类提案 v2，也不改 `skills.review`、覆盖层或规范化原文。

词表从当前代码导出。`topic_id` 为空只表示这份快照没有单独的专题编号，不表示专题不存在。历史 155 与运行时允许对不是同一个数，导出结果不增删专题去凑这两个数。待细分只表示领域已定、专题未定。

已有任务标签原样继承，并标成 `legacy_unverified`。空的 `tags` 对象和空的输入列表不算已覆盖，也不写成不适用。工具角色里的例子、禁止和替代不能当成 required。同一任务内才允许把动作和工具拼在一起筛选。

没有当前独立审核时资料等级是 U，不是低分。审核必须是同一固定模型的另一个 session 和另一份上下文，记为 `same_model_new_session`。历史审核账本缺失是 `untracked`，不是 `not_run`。

```bash
python3 library/enrichment.py taxonomy --out /tmp/atlas-taxonomy.json \
  --runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json
python3 library/enrichment.py map --database data/library/catalog.sqlite3 --out data/library/enrichment --dry-run
python3 library/enrichment.py pilot --database data/library/catalog.sqlite3 --limit 48
```

`taxonomy` 和 `pilot` 不调用模型。`pilot` 只列出最多 48 条身份。代码词表现在是 128 对；c26 运行时快照是 163 对，多出的 35 对都是路径派生的法律专题，全部保留，不按 128 丢弃，也不凑 155。

真正会调用本机 OpenCode 的是 `run`。它先生成，再换一个新 session 做独立审核，两段不会同时各开 10 路。云端测试只用假 CLI。

```bash
python3 library/enrichment.py run \
  --opencode /Users/milin/.opencode/bin/opencode \
  --database data/library/catalog.sqlite3 \
  --out data/library/enrichment \
  --phase both \
  --limit 48 \
  --max-requests 96 \
  --max-runtime-seconds 1500 \
  --workers 10 \
  --cost-policy allow-missing-cost \
  --after 7d912fe0474ddca4334c13288287bc0e
```

前置条件：`e90b125` 已装上，OpenCode 仍是 `opencode-go/iceman-contributor-free`，四项报价为 0，并且 8765 不要为了这次去重启。缺 `cost` 记 unknown，不写成 0，也不因此停。明确非 0、身份不可核或 tool 事件仍会停。`--dry-run` 的 map 不写 sidecar；`--apply` 才替换 `current.json`。回滚先不加 `--apply`。现站筛选出现在本机页面的「标签维 / 标签状态 / 标签值 / 第二维 / 同一任务内」。没有 apply 过 sidecar 时，这些条件不会改掉原列表。
