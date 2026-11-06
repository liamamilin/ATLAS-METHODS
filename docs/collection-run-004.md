# 现有队列连续采集报告：第 004—017 批

日期：2026-11-06（Asia/Shanghai）。本轮只处理 `sources.next.json` 原有 332 个来源，连续运行 14 批，不在批次之间等待确认。未新增候选或发现渠道，未刷新历史来源，未追补历史 70 个未导出入口，未修改采集器、合并器或核验脚本。

## 实际新增与累计成果

- 332 个来源全部得到本轮处理结果：318 个文本入口完整、6 个部分保存、7 个固定版本无入口、1 个 Git 访问失败。
- **新增 324 个有已保存正文的仓库来源、12,259 条 Skill 入口。** 本轮不同正文为 10,355 种，其中 33 种旧库已有，**净新增不同正文 10,322 种**。
- 累计 **373 个有正文的来源、24,345 条已保存入口、16,357 种正文**。累计完整来源 365、部分保存 8、无入口 12、失败 1。
- 累计发现入口路径 24,444 个，保存 24,345 个；99 个未导出入口包括历史 70 个和本轮 29 个符号链接，不计入保存数。
- 完全相同正文累计 4,627 组，涉及 12,615 条记录，超过每组首条的记录为 7,988 条；每一个来源都保留，没有名称或语义去重。
- 本轮保持每个来源一个固定 commit，入口按仓库 + 路径计数，已保存入口版本记录按仓库 + commit + 路径计数，两者均为新增 12,259、累计 24,345；仓库版本数量不当作 Skill 数量。

## 批次进度与全局队列口径

| 批次 | 选入 | 完整 | 部分 | 无入口 | 失败 | 保存入口 | 本批待处理 | 检查点全局未扫描 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 004 | 24 | 24 | 0 | 0 | 0 | 1,509 | 0 | 308 |
| 005 | 24 | 24 | 0 | 0 | 0 | 318 | 0 | 284 |
| 006 | 24 | 22 | 1 | 1 | 0 | 2,673 | 0 | 260 |
| 007 | 24 | 23 | 0 | 0 | 1 | 317 | 0 | 237 |
| 008 | 24 | 23 | 0 | 1 | 0 | 348 | 0 | 213 |
| 009 | 24 | 24 | 0 | 0 | 0 | 1,080 | 0 | 189 |
| 010 | 24 | 24 | 0 | 0 | 0 | 765 | 0 | 165 |
| 011 | 24 | 23 | 1 | 0 | 0 | 313 | 0 | 141 |
| 012 | 24 | 24 | 0 | 0 | 0 | 824 | 0 | 117 |
| 013 | 24 | 23 | 1 | 0 | 0 | 205 | 0 | 93 |
| 014 | 24 | 24 | 0 | 0 | 0 | 679 | 0 | 69 |
| 015 | 24 | 23 | 1 | 0 | 0 | 515 | 0 | 45 |
| 016 | 24 | 22 | 1 | 1 | 0 | 754 | 0 | 21 |
| 017 | 20 | 15 | 1 | 4 | 0 | 1,959 | 0 | 1 |

**“本批待处理”是已选入该批次、尚未产生处理结果的来源；失败单独统计，不算仍在等待执行。所有批次的该值均为 0。**

**“全局未扫描队列”是还没成功完成 Git 树扫描的来源，包括尚未开始的来源和扫描前失败的来源。** 表中该列是每批结束时的检查点数量，不是本批待处理数量，也不是最终队列数量。

本轮结束时：全局未扫描 **1**，其中从未尝试 **0**，失败未能扫描 **1**，即 `cbrock84/headcount`。它同时出现在 `sources.next.json` 和 `sources.retry.json`，不能相加算成两个来源。候选总数仍为 387，包含历史已处理来源及发现索引，不等于本轮新增来源。

[运行记录](../data/queue-run-004.json) 保存初始队列、逐来源结果、各批检查点及最终全局计数；每个批次的 `collection-report.json` 使用 `pending_sources_scope` 和 `global_unscanned_sources_at_checkpoint` 明确口径。

## 失败、无入口及部分保存

`cbrock84/headcount` 的 Git 克隆失败，原始错误为：

```text
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

该来源未解析到 commit，未扫描文件树，也没有正文计入成果。没有推断仓库已删除，没有替换来源或版本，记录失败后继续了后续来源。结果见第 007 批 `source-results.json`。

以下 7 个来源均已固定实际完整 commit，并扫描完整文件树，结果没有 SKILL.md，因而跳过，不凭仓库名称或索引介绍计入成果：

- `hanfang/claude-memory-skill`
- `rootlyhq/rootly-mcp-server`
- `devanasolver/ai-seo-toolkit`
- `hesreallyhim/awesome-claude-code`
- `travisvn/awesome-claude-skills`
- `VoltAgent/awesome-codex-subagents`
- `VoltAgent/awesome-openclaw-skills`

以下来源的可读正文已保存，另有 29 个入口为符号链接，按原规则不跟随：

| 来源 | 发现入口 | 保存入口 | 未导出原因 |
|---|---:|---:|---|
| career-ops-hq/career-ops | 15 | 9 | 6 个符号链接 |
| hasuwini77/3d-logo-skill | 2 | 1 | 1 个符号链接 |
| thedotmack/claude-mem | 44 | 42 | 2 个符号链接 |
| elithril/blender-kiln | 2 | 1 | 1 个符号链接 |
| ChatCut-Inc/agent-plugin | 126 | 108 | 18 个符号链接 |
| 0xNyk/council-of-high-intelligence | 2 | 1 | 1 个符号链接 |

部分保存来源的采集退出码 1 原样保留，没有放宽规则以凑全部成功。原文、脚本、参考材料及模板文本都只作为研究材料，没有安装、执行或将其指令作为当前任务指令。没有进行分类、方法提炼、内容摘要、评分或网站工作。

## 保存位置与关联材料

原文持久保存在本机项目 `/Users/milin/Documents/ChatGPT/ATLAS-METHODS/`：

```text
data-batch-004/ ... data-batch-017/
  repositories/<owner>/<repo>/raw/<owner>/<repo>/<commit>/
```

直接打开或复制各来源的 `raw/` 目录即可取回，不需要再次下载。旧 `data/raw/`、第 002、003 批原文保持原位。累计索引中的 `raw_path` 和 `repository_manifest` 相对于 `data/cumulative/`，`collection_root` 指向原来源输出目录；合并没有迁移或删除原文。

本轮保存 **228,830 个文本文件、3,391,585,672 字节**。沿用 UTF-8 文本快照及默认 2 MiB 上限，另有 **13,285 个仓库树条目**仅保留清单：11,657 个非 UTF-8 / 二进制条目、695 个字体、434 个符号链接、489 个超限条目、10 个子模块。上面的 29 个入口链接包含在这些遗漏中，不重复相加。

每个来源 `manifests/` 保存具体路径、类型、大小、Git blob / gitlink、不可变链接和遗漏原因。原始正文、frontmatter 与已有许可线索保留；未知值不补造，也不声称资产或运行依赖已补齐。

提交的是原有程序所在项目树、冻结来源清单、逐来源及批次索引、文件清单、累计索引和报告。沿用 `.gitignore`，原文、Git 缓存及临时文件没有公开批量上传。

## 验证与旧成果保留

执行原有自检，退出码 0：

```bash
python3 collect_skills.py --self-test
```

每个来源先解析完整 commit 并写入相应 `sources.batch-NNN.json`，扫描 Git 树后再调用已有采集命令。例如：

```bash
python3 collect_skills.py --sources sources.batch-004.json --only degausai/wonda --out data-batch-004/repositories/degausai/wonda
```

每个批次保存后调用原有核验器，14 批全部通过，`verification-report.json` 位于各批目录中。累计合并仍使用原有函数，等价调用如下，状态文件保留失败、无入口和部分结果：

```python
from pathlib import Path
from merge_collections import merge
merge([Path('data')] + sorted(Path('.').glob('data-batch-*/repositories/*/*')),
      Path('data/cumulative'), sorted(Path('.').glob('data-batch-*/source-results.json')))
```

最终累计核验通过，覆盖 **306,823 个 Git 树条目、289,814 个已保存文本文件、3,973,238,043 字节**，正文及关联文本逐字节与固定 Git blob 一致，索引和清单路径有效：

```bash
python3 tests/verify_collection.py --sources data/cumulative/sources.lock.json --out data/cumulative
```

本轮开始前的 12,156 条索引记录全部保留，字段和路径未改变；其中原有 12,086 条已保存正文仍可访问，未重采历史来源。已有程序未改变，因此没有新增测试。

实际对固定版本的 `degausai/wonda` 重跑，复用本地 Git 对象，再次累计合并，以第一次累计核验结果为基线运行：

```bash
python3 tests/verify_collection.py --sources data/cumulative/sources.lock.json --out data/cumulative --baseline-report /tmp/atlas-run004-final-baseline.json > data/cumulative/verification-report.json
```

检查通过，剔除运行时间戳后结果一致，未新增重复计数或丢掉其他成功来源。一次过早读取尚未写完的首次核验输出引发 JSON 检查错误，随后等待原命令退出码 0 再读取；数据未受影响，最终完整核验和重跑检查均通过，没有将该次读取错误当作采集失败或验证通过。

最终 `git diff --check`、原文和缓存忽略规则检查通过，旧批次及程序文件没有改动。

## 下一次继续

现有队列中的 332 个候选已全部尝试；唯一剩余来源是上面的访问失败，不是尚未轮到执行。访问恢复后，可单独重试：

```bash
python3 collect_skills.py --sources sources.retry.json --only cbrock84/headcount --out data-batch-007/repositories/cbrock84/headcount
```

重试后根据新报告更新第 007 批对应的 `source-results.json`、冻结清单及全局队列状态，再调用已有合并器和核验器。不要刷新其他已完成来源，也无需先解决历史 70 个未导出入口。本轮结束于现有候选的真实原文采集和可取回保存，没有新增发现渠道或进入内容处理阶段。
