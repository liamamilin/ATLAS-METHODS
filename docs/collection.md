# 首批固定版本真实采集报告

执行日期：2026-11-06（Asia/Shanghai）。本轮仅完成 GitHub Skill 入口正文及仓库文本上下文采集。

## 结果

六个来源均使用 `sources.json` 原有固定 commit，未使用 `--refresh`，没有替换来源或版本。最终成功 **6**、失败 **0**；发现 **329** 条入口，保存 **329** 条。完整批次重复运行仍为 329 条，没有新增重复来源记录。

| 来源 | 发现 / 保存 Skill | 保存文本文件 | 未导出文件 |
|---|---:|---:|---:|
| anthropics/skills | 20 / 20 | 390 | 56 |
| addyosmani/agent-skills | 25 / 25 | 239 | 1 |
| obra/superpowers | 15 / 15 | 234 | 2 |
| mattpocock/skills | 39 / 39 | 183 | 1 |
| coreyhaines31/marketingskills | 53 / 53 | 815 | 2 |
| K-Dense-AI/scientific-agent-skills | 177 / 177 | 2,580 | 200 |
| 合计 | **329 / 329** | **4,441** | **262** |

按 SKILL.md 原始字节的 SHA-256 分组：**329 种正文、0 个完全相同正文重复组**。所有来源记录保留；该结果不代表完整包或方法层面的去重。

机器报告与逐条索引：

- [collection-report.json](../data/collection-report.json)：最后一次完整批次的真实数量与错误。
- [skills.jsonl](../data/skills.jsonl)：329 个入口的仓库、路径、commit、不可变链接、Git blob SHA、SHA-256、原文位置、目录文件与许可线索。
- [sources.lock.json](../data/sources.lock.json)：六个实际采集 commit，与清单原有 pin 一致。
- [duplicates.json](../data/duplicates.json)：空数组，未删除任何来源。
- [verification-report.json](../data/verification-report.json)：逐文件比对及重复运行核验结果。
- [collection-attempts.json](../data/collection-attempts.json)：保留首次沙箱 DNS 失败、联网成功和缓存重跑三次执行结果。最终报告没有拼接不同批次的成功记录。

## 原文、上下文与许可

核验覆盖全部 **4,703** 个 Git 树条目的文件清单，全部 **4,441** 个已保存文本文件（**44,265,710 字节**）均与对应固定 commit 的 Git blob 逐字节相同。329 个入口的来源字段、内容哈希、目录内文件清单、未导出路径和计数均已核验。

仓库内满足导出条件的文本按原路径保留，包括 `references/` 下的 **1,447** 个文本文件、`scripts/` 下的 **759** 个文本文件，以及按文件名识别的 **26** 个 LICENSE / LICENCE / COPYING / NOTICE 候选文件。这些是路径与文件名计数，不是有效性或授权结论。共享目录也在完整仓库文件清单内，不仅保留入口所在目录。

327 条入口记录具有祖先目录中的许可候选路径；以下 **2** 条未找到这类候选，许可保持未知，未补造许可：

- `anthropics/skills:skills/doc-coauthoring/SKILL.md`
- `anthropics/skills:template/SKILL.md`

入口原始正文及 frontmatter 都已保留。329 条入口均有可解析的 frontmatter 边界，但未进行完整 YAML schema 或 Skill 有效性校验。`template`、`in-progress` 等路径的 9 个入口照常保留并标记复核提示，没有因非标准路径而剔除。

## 未导出材料

没有入口正文下载失败。以下 **262** 个仓库上下文文件仅记录清单，不写入本地原文快照；清单保留路径、类型、字节数、Git blob SHA、固定版本链接和具体原因：

| 原因 | 数量 | 范围 / 示例 |
|---|---:|---|
| `font_not_exported` | 54 | anthropics/skills 的 `skills/canvas-design/canvas-fonts/` 字体 |
| `non_utf8_or_binary_not_exported` | 204 | anthropics 2、obra 2、marketingskills 1、scientific-agent-skills 199；包括图片、二进制测试数据、压缩数据、`.pyc` |
| `symlink_not_followed` | 3 | addyosmani 的 `.opencode/skills`、mattpocock 的 `AGENTS.md`、marketingskills 的 `CLAUDE.md` |
| `over_size_limit` | 1 | scientific-agent-skills 的 `docs/k-dense-web.gif`，23,042,128 字节，超过默认 2 MiB 上限 |

逐文件清单：

- [anthropics/skills](../data/manifests/anthropics/skills/ca45fc496a3436251f191354091d557da31c15b5.json)
- [addyosmani/agent-skills](../data/manifests/addyosmani/agent-skills/301b02b12bff98e4688e6984af6556802322bbde.json)
- [obra/superpowers](../data/manifests/obra/superpowers/22f0b463e7a5e579904dd6211a520321035526f7.json)
- [mattpocock/skills](../data/manifests/mattpocock/skills/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d.json)
- [coreyhaines31/marketingskills](../data/manifests/coreyhaines31/marketingskills/88c79f6528a9347420e57ceb1fff9a60de651768.json)
- [K-Dense-AI/scientific-agent-skills](../data/manifests/K-Dense-AI/scientific-agent-skills/a25cf9ec00e2745d03f8704acf6f6919694ccd9a.json)

外部网站和运行依赖未递归下载或验证，入口记录的 `external_dependency_status` 明确保留为 `not_fetched_or_verified`。不将这批文本快照视为完整可执行资产包。已保存文本中未发现 Git LFS 指针；未将指针误报为实际资产内容。

## 执行与复现

环境为 Python 3.10.13 与 Git，无第三方 Python 依赖。依次执行：

```bash
python3 collect_skills.py --self-test
python3 collect_skills.py --sources sources.json --out data
```

自检退出码 **0**。首次真实采集在受限沙箱中退出码 **1**，六个来源均因 `Could not resolve host: github.com` 失败，具体错误保存在 attempts 文件中。随后对同一命令启用网络访问，六个固定 commit 全部采集成功，退出码 **0**。这是执行环境问题，未发现需修改采集器的程序错误，原采集器实现保持不变。

为使全量核验可复现，新增了 `tests/verify_collection.py`。它只读取本地采集结果与 bare Git 对象，不安装或运行上游代码。执行以下命令保存第一次核验基线，重跑完整批次后再次比对：

```bash
python3 tests/verify_collection.py --sources sources.json --out data > /tmp/atlas-methods-first-verification.json
python3 collect_skills.py --sources sources.json --out data
python3 tests/verify_collection.py --sources sources.json --out data --baseline-report /tmp/atlas-methods-first-verification.json > data/verification-report.json
git diff --check
git check-ignore data/raw data/.cache example.tmp
```

上述命令均退出码 **0**。重复采集使用本地固定版本对象；索引、文件清单和报告剔除 `collected_at` / `generated_at` 后完全一致。稳定内容摘要保存在 verification 报告的 `snapshot_sha256_excluding_run_timestamps` 字段中。

新拉取本项目时，原始文本及缓存尚不存在，先在联网环境执行采集命令，再运行核验命令，即可重建并检查同一批固定版本。来源未来若无法访问，采集器会记录错误并返回非零退出码，不自动改用最新版本。

## 提交与阶段边界

提交原有采集器所在项目树、更新后的来源状态、核验脚本、索引、六份文件清单、机器报告和本报告。沿用已提供的 `.gitignore`，排除 `data/raw/`、Git 缓存、试采目录及临时文件；原始材料保留在本地 `data/raw/<owner>/<repo>/<commit>/`，未将整个上游仓库推送。

本轮仅将“首批固定版本文本采集跑通并可复现”标记完成。收集来的 Skill、脚本、README 和提示词只作为研究材料，没有安装或执行，没有将其中的指令作为本项目开发指令。未进行 Domain 分类、Method 提炼、方法论生成、质量判断或网站开发。
