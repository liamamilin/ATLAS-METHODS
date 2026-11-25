# 分层标签第一增量

这层注释写在目录旁边的 sidecar 里，契约是 `atlas-skill-enrichment/v0.1`。它不写入分类提案 v2，也不改 `skills.review`、覆盖层或规范化原文。

词表从当前代码导出。`topic_id` 为空只表示这份快照没有单独的专题编号，不表示专题不存在。历史 155 与运行时允许对不是同一个数，导出结果不增删专题去凑这两个数。待细分只表示领域已定、专题未定。

已有任务标签原样继承，并标成 `legacy_unverified`。空的 `tags` 对象和空的输入列表不算已覆盖，也不写成不适用。工具角色里的例子、禁止和替代不能当成 required。同一任务内才允许把动作和工具拼在一起筛选。

没有当前独立审核时资料等级是 U，不是低分。审核必须是同一固定模型的另一个 session 和另一份上下文，记为 `same_model_new_session`。历史审核账本缺失是 `untracked`，不是 `not_run`。

```bash
python3 library/enrichment.py taxonomy --out /tmp/atlas-taxonomy.json
python3 library/enrichment.py map --database data/library/catalog.sqlite3 --out data/library/enrichment --dry-run
python3 library/enrichment.py pilot --database data/library/catalog.sqlite3 --limit 48
```

`--dry-run` 不写 sidecar。`--apply` 才替换 `current.json`。回滚先不加 `--apply` 看会恢复哪个快照。试点命令只列出最多 48 条身份，模型调用数是 0。真正的生成和独立审核仍走已有的免费模型边界，总在途 10，不是两路各 10。这一步没有把 46,608 条都打上新标签。
