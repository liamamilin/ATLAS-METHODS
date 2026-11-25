# 本机目录分类覆盖层

把已经看过的目录线索写成持久分类。重建本机目录时读回这份覆盖层。覆盖层只改浏览用的领域和专题。

它不改 SkillRecord，不改任务、适用条件、来源、副本关系或 `review`。详情里的审阅状态仍来自转换记录。新的目录归类显示为「未人工审阅 · 目录归类，未审源文」。这不是源文审阅，也不表示方法有效。`source`、`rule`、`sample` 这三种既有标签，只有提案里的预期原值与当前目录一致时才允许改领域或专题，而且标签本身保持不变。

公开目录不会在导入或重建时自动生成。不要为本批分类运行 `library/export_pages.py`，不要推送 `gh-pages`。

## 身份与文件

代表身份与目录里的 `skills.id` 相同，是 `sha256(collection_id + NUL + commit_sha)` 的前 32 位十六进制。这也是该代表在 `sources.id` 中的值。

覆盖层路径：

`data/library/classification-overrides.json`

这个文件在 `.gitignore` 的 `/data/library/` 里，留在本机。提案 JSON 也留在本机，不要提交那份完整提案。

重建时只有「这条来源仍是当前代表，且钉住的仓库、路径、提交、blob、入口哈希，以及当时的标题、摘要、任务目标与适用条件都没变」才套用覆盖。代表换成同组里的另一条，或内容已经变了，就跳过并在构建结果的 `classification_overrides.suspended` 里写原因，不把旧分类转给新代表。

路径型法律专题（例如 `general`、`securities`、`trusts and estates`）不是新专题。只有当前本机目录里已经出现过这对领域和专题时才接受。静态词表里的专题，包括「待细分」，可以直接使用。

## 只读预览

在本机仓库根目录执行。下面的路径按当前这台 Mac 的项目位置书写；提案文件不要放进 Git。

```bash
cd /Users/milin/Documents/ChatGPT/ATLAS-METHODS
```

把提案路径换成你保存 `all-454-review-proposals.json` 的位置。

```bash
python3 library/import_classifications.py \
  --proposals /绝对路径/all-454-review-proposals.json
```

默认不写覆盖层，不改 `data/library/catalog.sqlite3`，不读也不写 `data/normalized`。标准输出是 JSON。

- `changes`：将要改的领域、专题和标签。`to.review` 与 `from.review` 相同。
- `retained`：`retain_unfiled`。这 27 条不会写入。
- `conflicts`：身份、版本、预期原分类、目录内容或词表不一致。这些条目跳过，不覆盖。
- `snapshot_notice`：目录 `as_of` 与提案快照时间不同。时间不同本身不整批拒绝；每条仍按身份和内容核对。
- 退出码 `0`：拟归类条目都能写入或已经写入，保留条目只是列出。
- 退出码 `1`：至少有一条拟归类被跳过。先看 `conflicts`。
- 退出码 `2`：提案或覆盖层文件无法读取。什么都不会写。

预览里如果 `pending_rebuild` 非空，覆盖层已经有这些条目，但 SQLite 还是旧分类。接着只需要重建。

## 写入这 427 条

先看完预览。确认冲突可以接受之后再写入。有冲突时，命令仍只写入没有冲突的条目，保留条目不写，然后以退出码 `1` 结束。

```bash
python3 library/import_classifications.py \
  --proposals /绝对路径/all-454-review-proposals.json \
  --apply
```

记下输出里的 `batch_id`。同一份提案再执行一次不会改写已有的相同覆盖；输出里这些条目的 `status` 为 `unchanged`。

已有不同分类、或覆盖层里该条被改过且与提案目标不同的，会记为 `override_conflict` 或 `local_edit`，不会被这次写入盖掉。要换成新目标，先按下面的回滚去掉这一批未改动的条目。

## 重建现有本机目录

```bash
python3 library/build.py
```

这一步只读已保存的转换结果，原子替换 `data/library/catalog.sqlite3`。它读取覆盖层，不删除覆盖层，不写 `data/normalized`，不停止正在进行的转换。`library/serve.py` 每次请求单独打开目录；重建完成后刷新 http://127.0.0.1:8765 即可，不必为了分类重启转换进程。

若转换正在原子替换某一条记录，这次重建可能把该条计入 `excluded`。覆盖层还在，转换空闲后可以再执行一次 `python3 library/build.py`。

不要接着运行 `python3 library/export_pages.py`。

## 核对

再跑一次预览。已经重建且内容未变时，拟归类条目应为 `unchanged`，`catalogue_matches` 为 true，`pending_rebuild` 为空。保留条目仍是 27。退出码只有在没有冲突时才是 `0`。

```bash
python3 library/import_classifications.py \
  --proposals /绝对路径/all-454-review-proposals.json
```

构建输出里的 `classification_overrides.applied` 是本次套上的代表数。`suspended` 列出未套上的身份和原因：`content_drift`、`identity_mismatch`、`label_changed`、`invalid_topic`、`not_representative`、`not_in_catalogue`。

用预览 `changes` 里的一条 `id` 看目录行。`review` 应仍是 `unreviewed`。

```bash
python3 - << 'PY'
import sqlite3
con = sqlite3.connect('file:data/library/catalog.sqlite3?mode=ro', uri=True)
skill_id = '这里换成预览 changes 中的 id'
print(con.execute(
    'SELECT id, category, topic, label_from, review FROM skills WHERE id = ?', (skill_id,)
).fetchone())
PY
```

页面上该条进入新的领域和专题。打开详情应看到「未人工审阅」，以及「目录归类，未审源文」或原先的样本/规则/来源标签。任务和来源关系应与重建前一致。

## 回滚这一批

回滚只删除该 `batch_id` 下、分类仍等于导入目标的条目。同一条若后来改了领域、专题或标签，以及其他批次的条目，都留在覆盖层里。下面第一条只预览。

```bash
python3 library/import_classifications.py --rollback --batch '这里换成 batch_id'
python3 library/import_classifications.py --rollback --batch '这里换成 batch_id' --apply
python3 library/build.py
```

`--batch` 可以写完整哈希，或至少 8 位且只对应一批的前缀。回滚不改 SQLite；重建之后，被删除的覆盖恢复为规则、样本或来源分类，被保留的人工修改和其他批次仍在。

## 本机已有其他改动时如何放入这套代码

只把分类覆盖层这一支的 `library` 与本说明合入工作区。不要合并分支，不要 `stash`、`reset` 或整支 `checkout`，以免盖掉 `data/normalized` 和正在使用的标准化补丁。

先确认工作区里的 `library` 和本说明相对 `83e82ea` 没有本地修改：

```bash
git diff --stat 83e82ea -- library docs/classification-overrides.md
git fetch origin cursor/classification-import-623e
git diff 83e82ea origin/cursor/classification-import-623e -- library docs/classification-overrides.md | git apply --check
git diff 83e82ea origin/cursor/classification-import-623e -- library docs/classification-overrides.md | git apply
```

`git apply` 失败就停，不要改用 `git checkout` 覆盖工作区。标准化文件和 `data/normalized` 不应出现在这次 diff 里。
