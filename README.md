# ATLAS-METHODS

从真实 GitHub Skills 出发，自下而上研究可复用的方法。本阶段只做第一步：**采集原始材料**。

不预设 Domain，不提炼 Method / Methodology，不生成摘要、质量评分、领域树或方法图谱。

## 当前状态

- 2026-11-06 已完成首批 6 个固定 commit 的联网文本采集，成功 6、失败 0。
- 实际发现并保存 329 条 Skill 入口，329 种不同正文，完全相同正文重复组为 0。
- 保存 4,441 个文本文件；262 个字体、二进制、符号链接或超限文件仅建立清单。
- 离线自检、全部已保存文件与 Git blob 的逐字节核验、完整批次重复采集检查均已通过。
- 详见 [首批采集报告](docs/collection.md) 与 `data/collection-report.json`。原始材料保留本地，不随索引提交。
- 先前对话中的 4 份正文样本未纳入本仓库，不计入正式采集结果。
- 没有安装或执行来源中的 Skill，也没有进行方法有效性验证。

接手任务见 [AGENTS.md](AGENTS.md)。

## 文件

| 文件 | 用途 |
|---|---|
| `collect_skills.py` | 批量采集原文、仓库文本上下文、文件清单与索引 |
| `sources.json` | 首批来源与固定 commit；另含后续发现入口 |
| `AGENTS.md` | 编码 Agent 的本阶段任务、边界和验收要求 |
| `.gitignore` | 排除缓存、原始快照及临时文件；保留采集索引和报告 |
| `tests/verify_collection.py` | 对照固定版本 Git 对象，核验完整文件清单、原文字节、索引和重复分组 |
| `docs/collection.md` | 首批真实采集结果、遗漏及可复现的验证命令 |

## 运行

需要 **Python 3.10+ 和 Git**。采集器无第三方 Python 依赖，不调用模型，不需要单独的 API Key。真实采集需要能够访问 GitHub。

```bash
python3 collect_skills.py --self-test
python3 collect_skills.py --sources sources.json --out data
```

先验证一个来源：

```bash
python3 collect_skills.py --sources sources.json --out data-trial --only anthropics/skills
```

需要另外采集当前默认分支，而不是重现固定版本时：

```bash
python3 collect_skills.py --sources sources.json --out data --refresh
```

默认使用 `sources.json` 中的 commit。固定版本无法访问时应记录失败，不静默换成最新版本。

**每次输出的索引和报告只表示该次所选来源。** 小范围试采请使用独立 `--out`，避免部分来源索引覆盖完整批次索引。`--refresh` 生成新版本快照，旧原文目录保留。

## 输出

```text
data/
├── skills.jsonl               # 每个实际发现的 SKILL.md 一条记录
├── sources.lock.json          # 本次成功来源的实际 commit
├── duplicates.json            # 完全相同入口正文的分组，不删除来源
├── collection-report.json     # 实际数量、成功、失败和错误
├── raw/<owner>/<repo>/<sha>/   # 仓库文本快照，保持原相对路径
├── manifests/<owner>/<repo>/  # 文件清单，含未导出文件
└── .cache/                    # bare Git 缓存，不提交
```

采集器读取 Git 对象，不 checkout 工作树、不安装依赖、不执行被采集脚本。原始材料中的指令不是当前 Agent 的执行指令。

默认导出不超过 2 MiB 的 UTF-8 文本。二进制、字体、符号链接、子模块和超限文件只建立清单，不导出。因此这是**正文与仓库文本上下文采集器**，不是完整资产打包器。

共享 `references/`、`scripts/`、README 和许可文件，只要满足文本导出条件，都按原路径保存。外部网站、外部依赖和子模块不会递归下载；依赖可执行性未验证。tests、fixtures、templates、deprecated 等路径中的候选保留并加复核提示。

## 数据与去重边界

记录包括仓库、文件路径、commit、不可变来源链接、Git blob SHA、SHA-256、原文路径、原始 frontmatter、目录内文件、未导出文件、许可候选路径和采集状态。

只对 **SKILL.md 字节级相同** 的正文分组。入口相同的两个目录仍可能有不同脚本和参考资料，不宣称它们是同一个完整 Skill 包，更不把它们当作同一个 Method。所有来源记录保留。

frontmatter 原文保留；脚本只提取简单 name 预览，不进行完整 YAML schema 校验。文件名是候选发现条件，不代表已经证明 Skill 有效或优质。

## 首批固定版本

来源 refs 于 2026-11-06 通过 GitHub 连接核验。

| 仓库 | commit |
|---|---|
| anthropics/skills | `ca45fc496a3436251f191354091d557da31c15b5` |
| addyosmani/agent-skills | `301b02b12bff98e4688e6984af6556802322bbde` |
| obra/superpowers | `22f0b463e7a5e579904dd6211a520321035526f7` |
| mattpocock/skills | `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d` |
| coreyhaines31/marketingskills | `88c79f6528a9347420e57ceb1fff9a60de651768` |
| K-Dense-AI/scientific-agent-skills | `a25cf9ec00e2745d03f8704acf6f6919694ccd9a` |

这是种子批次，不是全 GitHub 的完整或代表性样本。来源中的两个发现索引不会被自动遍历，也不计入已采集内容。

## 完成判据

先看 `data/collection-report.json`：来源失败数为零，保存入口数与发现数一致；遗漏资产在文件清单中明确列出。采集失败或入口未保存时返回非零退出码，不报告成功。

检查原文可通过 commit 与 blob SHA 回溯，抽查入口正文及共享参考文件。重复运行不应产生重复来源记录。报告实际数量，不把来源仓库数量、Skill 条目数量和方法数量混为一谈。

离线自检覆盖正文一致性、共享参考文件、完全相同正文分组、二进制与字体仅索引、脚本不执行、非标准候选保留、重复导出和 JSONL 输出。首批联网文本采集及重复运行已验证；真实 Skill 执行和方法质量评估不属于本轮。

完成真实采集后，可使用本地 bare Git 缓存核验所有已保存文件及索引：

```bash
python3 tests/verify_collection.py --sources sources.json --out data
```

核验需要本地 `data/raw/` 和 `data/.cache/`；从 GitHub 新拉取本项目后，先运行采集命令重建它们。核验脚本不会运行收集来的代码。

## 提交范围

可以提交采集器、来源清单、测试、索引和报告。Git 缓存及原始快照默认不提交。原始材料保留各自许可，不统一改成 ATLAS 的许可；公开发布快照前逐来源检查许可和附属材料。采集器只记录许可线索，不自动作授权结论。
