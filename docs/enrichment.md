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

`pilot` 不加 `--ids` 时只打印领域抽样，`read_by_run` 为 false，`run` 不读它。`pilot --ids` 和 `manifest --ids` 用同一选择器：按逗号输入的顺序选出这 1 到 48 个 id，重复、缺失或超过 48 个就退出，不改去取默认前 48 条。命令把清单写到 `--out`。

`source_hash` 是规范 JSON `{"summary": skills.summary, "tasks": [该代表的 catalog_tasks 行]}` 的 sha256。规范 JSON 对对象键排序、保留数组顺序，分隔符是逗号和冒号。每个任务行含 `id`、`position`、`goal`、`applicability`、`outputs`、`tags`。这不是规范化 SkillRecord 文件的 sha256。文件里的 `normalized_record_sha256` 固定为 null，不参加 `sha256`。`sha256` 是同一套规范 JSON，对象只含 `ids` 和 `records`。`records` 与 `ids` 同序，`records[i].id` 必须等于 `ids[i]`。

```bash
python3 library/enrichment.py manifest \
  --database data/library/catalog.sqlite3 \
  --ids id-1,id-2,id-3 \
  --out slice.json
```

`--ids` 里的顺序会原样写入 `ids`。`id-1,id-2,id-3` 换成这次分片的真实代表 id，`slice.json` 换成这次要保存的路径。先导出，再把刚写出的文件交给 `run`。`run` 不接受 `--ids`、`--after` 或 `--limit`。清单 sha256、顺序、缺失 id 或当前 `source_hash` 不一致时，进程在任何模型请求之前退出。

首批按 1 条、9 条、38 条三份互不重叠的清单跑。每份用自己的 `--out` 目录。三份合计最多 96 次请求。单条和 9 条用 `--workers 1`，38 条用 `--workers 10`。

```bash
python3 library/enrichment.py run \
  --opencode /Users/milin/.opencode/bin/opencode \
  --database data/library/catalog.sqlite3 \
  --manifest slice.json \
  --out slice-out \
  --phase both \
  --workers 1 \
  --max-requests 2 \
  --max-runtime-seconds 1500 \
  --cost-policy allow-missing-cost \
  --runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json
```

9 条把 `--max-requests` 改为 18，`--workers` 仍为 1。38 条把 `--workers` 改为 10，`--max-requests` 改为 76。`--manifest` 和 `--out` 分别换成该分片刚导出的文件和该分片自己的目录。这三步都会调用模型。离线只做 `manifest` 导出。

`--runtime-allowlist` 对 `taxonomy`、`map` 和 `run` 是同一个参数。不传时不会因为专题不在代码里的 128 对中而拒绝记录。

8765 上的进程在启动时加载 `library/serve.py`。`library/web/index.html`、`style.css`、`app.js` 每次请求从磁盘读取；接口代码不会热更新。父级要在同一端口重启原来的进程，不要另开一个服务。端口已被占用时 `serve.py` 会直接退出。重启前先停掉现有进程，再在仓库根目录执行 `python3 library/serve.py --port 8765`。没有 sidecar 时，带 `edim`、`estate`、`evalue`、`edim2`、`evalue2` 或 `esame` 的列表是空的；不带这些参数的旧列表保持原样。这次交付不重启该进程。
