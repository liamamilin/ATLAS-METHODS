# 分层标签第一增量

这层注释写在目录旁边的 sidecar 里，契约是 `atlas-skill-enrichment/v0.1`。它不写入分类提案 v2，也不改 `skills.review`、覆盖层或规范化原文。

词表从当前代码导出。`topic_id` 为空只表示这份快照没有单独的专题编号，不表示专题不存在。历史 155 与运行时允许对不是同一个数，导出结果不增删专题去凑这两个数。待细分只表示领域已定、专题未定。

已有任务标签原样继承，并标成 `legacy_unverified`。空的 `tags` 对象和空的输入列表不算已覆盖，也不写成不适用。工具角色里的例子、禁止和替代不能当成 required。同一任务内才允许把动作和工具拼在一起筛选。

没有当前独立审核时资料等级是 U，不是低分。审核必须是同一固定模型的另一个 session 和另一份上下文，记为 `same_model_new_session`。历史审核账本缺失是 `untracked`，不是 `not_run`。

```bash
python3 library/enrichment.py --help
python3 library/enrichment.py taxonomy --out /tmp/atlas-taxonomy.json \
  --runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json
python3 library/enrichment.py map --database data/library/catalog.sqlite3 \
  --out data/library/enrichment \
  --runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json \
  --dry-run
python3 library/enrichment.py pilot --database data/library/catalog.sqlite3 --limit 48
```

`taxonomy` 和 `pilot` 不调用模型。`pilot` 只列出最多 48 条身份，`run` 不读取这份列表。代码词表是 128 对，c26 运行时快照是 163 对。多出的 35 对全部是法律专题，已由本机对照确认；代码里的 128 对都在这 163 对里。161 个已观察到的分类对也都在其中。运行时有、当前观察里还没有的是 `integration/待细分` 和 `legal/税务`。不按 155 或 163 去增删专题。

`map` 对 `skills` 和 `catalog_tasks` 各读一次。它不按代表逐条重扫任务表，也不把 `sources` 逐行读进来。`--dry-run` 不写 sidecar。标准输出里的 `complete: true` 才表示这次把目录里的全部代表都映射完了；带 `--limit` 或 `--after` 的结果是有界片段，`complete` 为 false。进度写在标准错误，每 5000 条一条 JSON。2026-11-25 那次本机全库 dry-run 在 180 秒被终止，没有覆盖统计，不能算通过。旧的 action/tool 有值覆盖由这次映射计数；八个新维度的值覆盖仍是 `unknown_not_computed`。

有界续跑使用同一次 `map` 命令，另加 `--limit` 和一个 `--after`。`--after` 的值取上一份结果里的 `next_after`，不是固定字符串。这样的结果 `complete` 为 false，不能当作全库通过。完整映射就是上面那条不带 `--after`、不带 `--limit` 的 dry-run。

`pilot` 不调用模型，`run` 也不读取它的输出。分层 48 条只有在 `--manifest` 指向父级自己的清单时才会被选中。`--after` 的默认值是空字符串，按代表 id 从整个目录往后取，包含已经分类、只是还没有标签的记录；它不会只取待归类余量。不要把 c26 的待归类游标传给这次运行。

`run` 会调用本机 OpenCode。先生成，再换一个新 session 审核。生成提示只含契约和当次目录文字；审核提示只含同一段目录文字和候选 quote，不含生成者的推理。两段共用 `data/library/atlas-writer.lock`，最多 10 路。缺 `cost` 在 `allow-missing-cost` 下记 unknown，不写成 0。明确非 0、身份不可核、价格不可核或 tool 事件会停；已经发出的请求可以记完，不再发新的。失败记入 `phase-manifest.json`，下次不自动重试，已覆盖游标停在失败之前。`run` 自己写入 sidecar，不需要再执行一次 map。

父级的 48 条清单不在这个仓库里，所以这里不写一条带假路径的 `run`。`python3 library/enrichment.py --help` 列出 `--manifest` 和 `--ids`。清单是 JSON，含 `ids`、`records`（每条有 `id` 和当前 `source_hash`）和 `sha256`。`sha256` 必须等于 `manifest_digest(ids, records)`。摘要不符、源文摘要不符或不在当前目录里的 id 会被拒绝，并且不会改去扫描其他代表。`--ids` 是逗号分隔的 1 到 48 个 id，不核 source hash。既定 48 条要加的参数只有 `--manifest` 加那份真实文件；同时可加的是 `--phase both`、`--max-requests 96`、`--max-runtime-seconds 1500`、`--workers 10`、`--cost-policy allow-missing-cost` 和 `--runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json`。这条会调用模型，离线验收不要执行。

不带 `--manifest` 时，`run` 按代表 id 从整个目录往后取，包含已经分类的记录。省略 `--limit` 时默认 48。要覆盖全部代表，省略 `--after`，并把 `--limit` 设成目录代表数、把 `--max-requests` 设成至少代表数的两倍。下面这条是 id 顺序的前 48 条，不是分层清单，也不是全库。它会调用模型，离线验收不要执行。

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
  --runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json
```

`--runtime-allowlist` 对 `taxonomy`、`map` 和 `run` 是同一个参数。不传时不会因为专题不在代码里的 128 对中而拒绝记录。

8765 上的进程在启动时加载 `library/serve.py`。`library/web/index.html`、`style.css`、`app.js` 每次请求从磁盘读取；接口代码不会热更新。父级要在同一端口重启原来的进程，不要另开一个服务。端口已被占用时 `serve.py` 会直接退出。重启前先停掉现有进程，再在仓库根目录执行 `python3 library/serve.py --port 8765`。没有 sidecar 时，带 `edim`、`estate`、`evalue`、`edim2`、`evalue2` 或 `esame` 的列表是空的；不带这些参数的旧列表保持原样。这次交付不重启该进程。
