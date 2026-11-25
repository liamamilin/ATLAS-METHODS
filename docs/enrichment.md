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

生成提示带上 `canonical-value/v1` 的规范值 id。`value_id` 只能取该维度列出的 id。词表里没有的词把 `value_id` 留空，新词放在 `proposed_value`，模型不能自己写成已接受。提示里的例子标了假例子，不使用用户材料。程序只在 quote 是摘要或任务里的连续原文、作用域没有跨到别的任务、并且该维度只对应一个规范 id 时，才补上缺失的 `value_id`。审核看到候选的 `value_id`、`role`、`scope` 和 quote。某一轴通过不会把所有候选都变成可筛选标签。供给只有摘要和任务，`source_fidelity` 不会保持 pass。已审定且有证据的规范值才写入维度 `values`。没有依据的维度保持 unknown。

已经生成的那一条不要自动重跑。若保存的生成响应里，quote 能无歧义对上规范 id，用下面的命令离线修复，不调用模型。写出的注解等级是 U，`prior_review_reusable` 为 false，旧的 D1 不能当作这次修复的审核。`--ids` 只放那一条代表 id，`--attempt` 是该条 `generate/response.txt`。

```bash
python3 library/enrichment.py repair \
  --database data/library/catalog.sqlite3 \
  --ids 那一条代表id \
  --attempt 该条generate/response.txt \
  --out repaired.json
```

剩下未请求的代表先取 1 条做下一道有效性门，通过后再取 8 条，然后是 38 条。三次合计仍在最初 96 次请求之内，不重置已用掉的次数。已经请求过的两条不要重跑。

可筛选值只来自逐事实 `decision` 为 `accept` 的候选。`unknown`、缺项、`reject` 和冲突都保持候选，不会因为规范值对得上或总轴通过而进入 `values`。明确写下的新词（例如 `proposed_value`）不会因为同一句里另有一个已知工具就被收成那个工具。

已保存的那一条如果把新词收成了别的规范值，候选语义已经变了。`replay` 对照 `--reviewed-annotation` 后会停在 U，不沿用旧 D1，也不接受任何事实：

```bash
python3 library/enrichment.py replay \
  --database data/library/catalog.sqlite3 \
  --ids 那一条代表id \
  --generation 该条generate/response.txt \
  --review 该条review/response.txt \
  --reviewed-annotation 当时保存的annotation.json \
  --out replayed.json
```

输出里 `replay_status` 为 `candidate_semantics_changed` 时，这份结果不能当作审核通过。

已完成独立审核的 run 目录用 `publish` 收成站点读取的 sidecar。不加 `--apply` 只预览。草稿、失败行和 `review_decision` 不是 `accept` 的事实不会变成可筛选值。等级 U 保持 U。等级 R 仍发布，但 `recommend` 为 false，不删除原文。目标里已有、这次没有带到的记录保持原样。同一条记录指纹不一致则整次不写。`--apply` 之后，`rollback --snapshot` 用发布结果里的 `previous`，先不加 `--apply` 预览，再加 `--apply` 才把 `current.json` 指回去。历史快照和账本保留。

```bash
python3 library/enrichment.py publish \
  --database data/library/catalog.sqlite3 \
  --out data/library/enrichment \
  --run 第1片run目录 --run 第7片run目录 --run 第38片run目录
python3 library/enrichment.py publish \
  --database data/library/catalog.sqlite3 \
  --out data/library/enrichment \
  --run 第1片run目录 --run 第7片run目录 --run 第38片run目录 \
  --apply
python3 library/enrichment.py rollback \
  --out data/library/enrichment \
  --snapshot 发布结果里的previous
python3 library/enrichment.py rollback \
  --out data/library/enrichment \
  --snapshot 发布结果里的previous \
  --apply
```

8 次已完成的审核请求对应 3 个 U、4 个 D1、1 个 R。这 3 个 U 是审核已经跑完、等级仍为 U，不是 3 次请求失败。另外 24 条是只生成、尚未审核的草案，也是 U，不能算进那 8 次。

38 片停在 `tool_or_transport`，但保存的工具事件是 0，3 次是 `provider.invalid-output`。工具事件、身份不可核、明确非 0 仍会停止。缺费用在 `allow-missing-cost` 下不单独造成这种停止。要继续，只审核已经生成成功的记录，不重试失败的 3 条，也不把未请求的 11 条算进完成：

```bash
python3 library/enrichment.py run \
  --opencode /Users/milin/.opencode/bin/opencode \
  --database data/library/catalog.sqlite3 \
  --manifest 该38片清单 \
  --out 该38片run目录 \
  --phase review \
  --workers 10 \
  --max-requests 24 \
  --max-runtime-seconds 1500 \
  --cost-policy allow-missing-cost \
  --resume-provider-invalid \
  --runtime-allowlist library/fixtures/taxonomy-reconciliation-c26.safe.json
```

`--resume-provider-invalid` 只解除 `tool_or_transport` 或 `provider_invalid_output`，并且已保存回合必须是该免费模型、没有工具事件、没有明确非 0。它不删除旧账，也不重跑 `phase=error` 的记录。11 条未请求要另用只含这 11 条的清单，在剩余请求额度内再跑，不能把首批 48 条说成已经全部完成。

8765 上的进程启动时加载当时的 `library/serve.py`。页面文件每次请求从磁盘读，接口要等这个进程重启才换。本机当前唯一进程是 PID 71194。这次只改事实去重和离线发布，不重启它，也不要另开第二个服务。

`--runtime-allowlist` 对 `taxonomy`、`map` 和 `run` 是同一个参数。不传时不会因为专题不在代码里的 128 对中而拒绝记录。

同一任务内对所有维度生效，不限动作和工具。两条事实的 `task_position` 不同就不能拼在一起。`level` 为 `skill` 的事实对整条 Skill 成立，所以可以和其中任意一个任务的事实组合；两条都在 skill 上时，勾不勾「同一任务内」结果相同。这不是跨任务拼出来的组合。

被卡住的那 48 条不要重跑模型。`publish` 只读 run 目录。完全相同、且原始审核对这个 `fact_id` 都是 `reject` 的候选会收成一条，`accepted` 仍为 false，其它已接受事实的决定保持原样。对不上就跳过该代表，其余照常预览或发布。`--exclude` 可显式跳过，源目录不改。

```bash
python3 library/enrichment.py publish \
  --database data/library/catalog.sqlite3 \
  --out data/library/enrichment \
  --run 该48条的run目录
python3 library/enrichment.py publish \
  --database data/library/catalog.sqlite3 \
  --out data/library/enrichment \
  --run 该48条的run目录 \
  --apply
```

预览的 `writes` 是 0，源 run 目录不改。`normalized_identical_rejects` 有那一条，就可以 `--apply` 整片 48。已发布的 142 条留在原快照里，一起进入新快照。若该条在 `skipped`，把代表 id 放进 `--exclude` 再发布其余 47。`--apply` 之后若要撤回，用计划里的 `previous`（当前指针的前一版是 `23ae4f011b0ba06f`）：

```bash
python3 library/enrichment.py rollback \
  --out data/library/enrichment \
  --snapshot 23ae4f011b0ba06f \
  --apply
```

不要新开这 48 条的审核。本观察批已请求 295，剩下 152 条最多 304 次，合计 599，不超过 600。旧的 2 条坏流水线和 4 条 provider invalid 不重试。这次不重启 8765。发布和后续 `run` 各自起进程，读磁盘上的 `enrichment.py`。正在跑的 PID 71194 继续提供已经安装的检索。

标签值的默认查询是「已审 accept 的这个规范值，任何角色」。`mentioned`、`example`、`prohibited` 都能被找到，详情里写出角色。`erole=required` 只保留 `required` 和 `conditional`。`erole=legacy` 只查旧任务标签，不把它们当成已审 accept。不带 `erole` 时，旧标签不会冒充已审值。这就是 `image` 的 mentioned 以前被漏掉、而 `sql` 仍能命中的原因：按值查询被固定成必需角色，同时 `tools`/`task_action` 的 `legacy_unverified` 又不看角色。当时那 43 条审核记录的等级是 D1 31、U 8、R 4。随后生产曾到 142/46608。2026-11-25 21:17 的只读核对是 242/46608、394 条 accepted、122 个有值代表，等级 D1 183、U 32、R 27，current `279f6eae5f2c38ce`，previous `f52ba82d5c332182`。这些等级和缺费用都不是高吞吐改造的原因。

8765 上的 PID 71194 在启动时加载了当时的 `library/serve.py`。页面文件每次请求从磁盘读取；接口代码不会热更新。这次的去重修复不需要动它。端口已被占用时再执行 `serve.py` 会直接退出，不要另开第二个服务。没有 sidecar 时，带 `edim`、`estate`、`evalue`、`edim2`、`evalue2` 或 `esame` 的列表是空的；不带这些参数的旧列表保持原样。

## 合批队列 v0.2

`run` 仍然是一条记录一次生成、一次审核。清单里有 48 条并不等于一个模型请求里有 48 条。`queue` 才把多条记录放进同一次 `opencode run`。旧的 stopped run 不会被 `queue` 接着跑。

字符数乘 2 是保守的估算 token，不是官方分词器，也不是字节数。共享词表只写一次。单条超长记录不截断正文：放不进预算就整条不送，记 `over_budget`。模型输出不是完整 JSON，或回合 `finish` 为 `length` 时，这一请求里的记录全部不接受。

规则和封闭短语只产生候选。审核接受之前，检索看不到它们。`reject`、`unknown`、缺决定、冲突、跨记录引用都不接受。否定句上的 required 会按原文改成 prohibited，不能借 required 通过。完全相同的事实仍合并。同一 `fact_id` 而角色或作用域不同则冲突，不接受。`source_fidelity` 在摘要加任务的供给上不能保持 pass。等级 R 的 `recommend` 为 false。

一次 `execute` 或 `resume` 自己从生成走到审核，并在每个请求后写 ledger。远程会话断开不影响已经写进 ledger 的计划；机器休眠或关机不在这个保证里。ledger 跨档位、跨阶段累计。硬上限是 80 次 `opencode run`、100 个实际 assistant turn、2700 秒、同时在途 10。先到即停，不再提交新请求。换一个 `--out` 不能清空这笔账，也不能绕过 tools、非 0、错误模型、身份不可核或已经写下的 timeout halt。缺费用在 `allow-missing-cost` 下仍是 unknown，不写成 0。未知结果不自动重发。一个记录失败不把同请求里已经成立的记录打回，也不整批重跑。

事实字段只能是 `dimension`。模型若写成 `category`，即使里面的词是 `kind` 或 `task_action`，也整条拒绝，不能改名后接受。假例子里的 quote 不是任何真实记录的原文。

队列请求的进程超时是这条计划还剩的运行时间，不是单条分类客户端的 180 秒。真的超时仍是全局 halt，相关记录保持 `unknown_outcome` 且 `will_not_retry`。已经停住的 ledger 再执行不会发新的模型请求，也不会先去核价。`--workers 10` 时，同一波生成或审核可以同时在途，但不超过封存的 10，并且有待审核时先审核。2026-11-25 23:00 那次首测在 `f97` 上只跑了 b8 和 b16：8 条生成因 `category` 被全部丢弃，b16 在约 180 秒被旧的单条超时掐断。halt 原因是 `timeout`。剩余 78 次请求、98 个 turn、2374.468 秒只是算术余量，不是清掉 halt 或重跑这 16 条的许可。那次没有进入审核、对照或留出，没有选出档位，也不能拿来比较吞吐或宣称非劣。

不同 arm 的缓存按计划和 arm 分开。不能用上一档的结果冒充下一档的新测量。请求数不是耗时，也不能把同一条记录在多个 arm 里的出现次数当成覆盖率分母。

基准计划读三份已经由 `manifest` 写出的官方清单：开发 48、留出 48、对照 16。对照必须是开发集的子集。开发档和留出档不会进同一个批次。`plan` 先按真实正文长度打包，给出每个 arm 的估算请求数；这个数不是测量。封存的选择规则是 `dev-quality-then-throughput/v1`：开发集上机械错审为 0 的档位里，取每秒审完代表更多的一档，平手则请求更少，再平手则更小的档。留出集只跑这一档，用来确认，不回头改规则或阈值。另外两档留在计划里，标记为未跑，不能说成 96 条三个档都测过。若打包后开发、对照再加留出确认放不进 80 次请求，计划会从最大档开始删，并写明原因；放不进就停，不把上限加到 80 以上。

开发集来自己发布记录，留出集在否定、条件和长度上更难。留出结果不能直接外推到 46608。准备说明的 sha256 只是备注，不是可执行计划。新的 ledger 是独立预算，不读取、不清除旧 run 的 transport halt，也不启动那 52 条。

`source_hash` 仍是摘要加任务行的规范 JSON。`outputs` 保持数据库里的 JSON 文本，不先解码成数组。`tags` 用解码后的 JSON。任务行不含 `skill`。这和已经导出的官方清单一致。

`queue` 不发布生产，也不重启 8765。预览的 `writes` 是 0。要进生产仍用原来的 `publish`，先不加 `--apply`。

下面的尖括号都是占位，不是本机路径。一次启动会先跑开发集和 16 条对照；开发集都结束后，同一进程按上面的规则决定是否继续留出集。中途退出就用 `resume`，不要换 ledger 或计划。

```bash
python3 library/enrichment.py queue --help
python3 library/enrichment.py queue \
  --action plan --benchmark \
  --database <catalog.sqlite3> \
  --dev-manifest <dev48.json> \
  --heldout-manifest <heldout48.json> \
  --control-manifest <control16.json> \
  --preparation-sha256 ee8dff6edbdb0bc63554057f6d74b8f9c63907ee5bd164998dffefdaebb92d5c \
  --cost-policy allow-missing-cost \
  --workers 10 \
  --ledger <new-benchmark-ledger> \
  --out <queue-plan.json>
python3 library/enrichment.py queue \
  --action validate \
  --database <catalog.sqlite3> \
  --plan <queue-plan.json> \
  --ledger <new-benchmark-ledger> \
  --out <validate.json>
python3 library/enrichment.py queue \
  --action execute \
  --database <catalog.sqlite3> \
  --plan <queue-plan.json> \
  --ledger <new-benchmark-ledger> \
  --out <staging-dir> \
  --opencode opencode \
  --cost-policy allow-missing-cost \
  --workers 10
```

`execute` 返回后可以用 `--action status` 看哪些 arm 实际跑过。留出档在选择之前是估算。staging 不是生产 enrichment 目录。这次没有真实模型调用，没有测 Mac 上的墙钟，也没有证明合批相对单条非劣。
