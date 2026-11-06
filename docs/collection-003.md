# 第三批 GitHub Skill 实际采集报告

日期：2026-11-06（Asia/Shanghai）。本轮原样复用采集器、合并器和核验脚本，**没有程序改动或新增审计流程**。

## 新增材料与累计数量

冻结的 24 个新仓库全部实际联网采集成功，入口正文全部保存；本轮失败 0、无入口 0、部分保存 0。每个仓库先固定完整 commit、扫描完整 Git 树（包括隐藏目录、插件及多层子目录），再调用现有 `collect_skills.py`。逐仓库写入原文、索引、清单、lock、报告与 tree-scan 后才继续下一个。

| 统计口径 | 本轮新增批次 | 累计已保存成果 |
|---|---:|---:|
| 有已保存正文的仓库 | 24 | 49 |
| Skill 入口：仓库 + 路径 | 1,242 | 12,086 |
| 已保存版本快照：仓库 + commit + 路径 | 1,242 | 12,086 |
| 不同正文：SHA-256 | 991 | 6,035 |
| 完全相同正文重复组 | 55 | 3,001 |

本批 991 种正文中，2 种在旧库已有，因此**首次增加到累计库的正文为 989 种**。没有按名称合并，也没有语义去重；重复正文保留每一个来源。当前每条已保存入口仅有一个采集 commit，故入口数与入口版本快照数相等。机器报告的旧字段 `indexed_snapshots` 为仓库版本数，不作为 Skill 入口版本快照数。

累计处理 54 个仓库：47 个文本入口完整、2 个历史部分保存、5 个历史固定版本无入口；最终仓库访问 / 导出异常失败 0。历史 70 条未导出入口继续留在索引中，未计入 12,086 条保存数。前两批的全部 10,914 条索引记录（其中 10,844 条有已保存正文）及原文保留，没有重采历史材料。待后续扫描的候选仓库为 **332** 个。

| 新来源（链接固定 commit） | 实际保存入口 |
|---|---:|
| [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill/tree/c1c8a8c1471069fb0e188eeaff69b8e8db6564a8) | 1 |
| [realkimbarrett/advertising-skills](https://github.com/realkimbarrett/advertising-skills/tree/45f4a4a1dabe24113193369b55b929b1de4ff04a) | 12 |
| [deanpeters/Product-Manager-Skills](https://github.com/deanpeters/Product-Manager-Skills/tree/cfcb697f14b8891d8b3ec43c16c3547df3e70e13) | 77 |
| [phuryn/pm-skills](https://github.com/phuryn/pm-skills/tree/2a12295a7717569baa7be181b4cfebe3ca38a6bb) | 69 |
| [makenotion/notion-cookbook](https://github.com/makenotion/notion-cookbook/tree/8a94d7faae9d21d908a18f4f4a3e9b30c561d3f0) | 246 |
| [makenotion/claude-code-notion-plugin](https://github.com/makenotion/claude-code-notion-plugin/tree/016c74ffc568920a385186fb9d2d9d2801e0be09) | 12 |
| [resend/resend-skills](https://github.com/resend/resend-skills/tree/9cf29b7c1dcc0ec00a183e3c715fa7cde606e068) | 5 |
| [redis/agent-skills](https://github.com/redis/agent-skills/tree/74708dab4b3f81a9845803c3f642a4f7d443af69) | 28 |
| [NVIDIA/skills](https://github.com/NVIDIA/skills/tree/c24aea9a0b0bbe9bd16363682045fc9218b3cbef) | 438 |
| [google/skills](https://github.com/google/skills/tree/c2cf4ba74c0e47f0e7a81d29e053e740998a0ae2) | 191 |
| [cypress-io/ai-toolkit](https://github.com/cypress-io/ai-toolkit/tree/5258ece52a0010d268946e08606c98784c98f255) | 5 |
| [qdrant/skills](https://github.com/qdrant/skills/tree/9f55f6a7fb225932da70c6e7dcd671c7e9a8929a) | 34 |
| [zilliztech/milvus-skill](https://github.com/zilliztech/milvus-skill/tree/7553e6c2b5940aef0f4a1aacc40d956745af0ac5) | 1 |
| [BrianRWagner/ai-marketing-claude-code-skills](https://github.com/BrianRWagner/ai-marketing-claude-code-skills/tree/f36b34fc539a7a3b801839bfff2c26277fd7ec69) | 23 |
| [AgriciDaniel/claude-seo](https://github.com/AgriciDaniel/claude-seo/tree/6be93f45064ffbadced572b5d5c95f55fd263af0) | 35 |
| [wshuyi/x-article-publisher-skill](https://github.com/wshuyi/x-article-publisher-skill/tree/eb3197d1d8579556aea5018d5c0b141d55fc2df6) | 1 |
| [CosmoBlk/email-marketing-bible](https://github.com/CosmoBlk/email-marketing-bible/tree/962648c1b147652244c4e4953e96c65421a79e70) | 2 |
| [smixs/creative-director-skill](https://github.com/smixs/creative-director-skill/tree/ac06eb255371da35d1db0a146dbfcca8c8857543) | 1 |
| [Xquik-dev/x-twitter-scraper](https://github.com/Xquik-dev/x-twitter-scraper/tree/23c895e82461e0efdca6bfb62e543b0144ab5f96) | 3 |
| [Xquik-dev/tweetclaw](https://github.com/Xquik-dev/tweetclaw/tree/ae9ba593dc6ed383bb631c018b965e59f6c66ff3) | 1 |
| [SHADOWPR0/beautiful_prose](https://github.com/SHADOWPR0/beautiful_prose/tree/3b8f98079eaf79a0013ccdbedbb332a5b0ba19e0) | 1 |
| [blader/humanizer](https://github.com/blader/humanizer/tree/0ef9fe5bc3572734fd3ae6c448fe00a30cd5dcf0) | 1 |
| [MohamedAbdallah-14/unslop](https://github.com/MohamedAbdallah-14/unslop/tree/eaa3b92e0b54a29076df23ff6d846c5f81bed7b9) | 15 |
| [appeeky/aso-skills](https://github.com/appeeky/aso-skills/tree/3919d7c27402d92c7ef698f2016174b874ea189e) | 40 |

## 来源发现与范围

使用现有 `sources.next.json` 中尚未处理的前 24 个仓库，先保存 [sources.batch-003.json](../sources.batch-003.json)，再开始采集。来源地址已按仓库去重，不使用领域或 Star 筛选，不凭仓库名称或介绍计入成果。运行期间未扩展本轮范围。

通过 GitHub 连接核对两个原有发现入口，其 HEAD 与此前保存的固定版本一致：

- VoltAgent/awesome-agent-skills：`6d483e29da745a010d88974ef6d0e50d15755e9a`。
- devanasolver/awesome-agent-skills：`e7e8e2e75b317c9573019f270aebfd8756a0a419`。

此前的两个 README 原文仍可访问且 SHA-256 校验通过，因此复用其提取结果与逐链接发现出处，不重复下载。原文位置见第二批 [indexes.json](../data-batch-002/discovery/indexes.json)。此前容易解析的 `skills.sh/<owner>/<repo>/<skill>` 已映射为仓库，其余未映射链接及原因继续保留在第二批发现记录中。

GitHub 搜索 `agent skills SKILL.md` 可用，返回 5 个命中，其中仅 `KiWi233333/JiwuChat`、`go-musicfox/go-musicfox` 是候选表中未有的仓库，已留给后续批次。来源候选由 385 增至 387；此前 354 个待处理减去本轮 24 个，再加 2 个新搜索候选，剩余 **332**。搜索命中只算候选，不算存在真实入口的证明。[checks.json](../data-batch-003/discovery/checks.json) 保留渠道结果；[repositories.json](../data-batch-003/discovery/repositories.json) 保留现成仓库信息，24 个均为 `fork=false`，不追查镜像谱系。

## 原文及关联文本

本轮持久保存 **12,682 个文本文件、116,581,545 字节**。原始 SKILL.md 字节不变，frontmatter、许可线索、参考文档、脚本和模板文本沿用已有字段与路径。48 条新入口没有祖先目录中的许可候选线索，保持未知，不阻塞正文保存，也未补造许可。

本轮没有入口正文遗漏。另有 **289 个仓库上下文条目**按现有规则未导出：

| 原因 | 数量 |
|---|---:|
| 非 UTF-8 / 二进制 | 151 |
| 符号链接不跟随 | 119 |
| 子模块不递归 | 16 |
| 超过默认 2 MiB | 2 |
| 字体 | 1 |

逐项路径、类型、字节数、Git blob / gitlink、不可变来源链接及未保存原因均在各来源的 `manifests/` 中，索引的 `repository_manifest` 可定位。不声称已补齐全部资产或运行依赖。所有上游 Skill、脚本、提示词及 README 仅作为研究材料，没有安装、执行或将其内容作为当前任务指令。

## 保存位置与取回

原文位于本机持久项目目录 `/Users/milin/Documents/ChatGPT/ATLAS-METHODS/`：

```text
data-batch-003/repositories/<owner>/<repo>/raw/<owner>/<repo>/<commit>/
```

直接打开或复制这些 `raw/` 目录即可取回本轮材料，不需要重新从上游下载。旧原文继续保存在 `data/raw/` 和 `data-batch-002/repositories/<owner>/<repo>/raw/`。未将整个上游快照推送到公开 GitHub。

- [本批索引](../data-batch-003/skills.jsonl)、[本批报告](../data-batch-003/collection-report.json)、[逐来源结果](../data-batch-003/source-results.json)。
- [累计索引](../data/cumulative/skills.jsonl.gz)、[累计报告](../data/cumulative/collection-report.json)、[累计重复分组](../data/cumulative/duplicates.json)、[累计核验](../data/cumulative/verification-report.json)。
- [已固定版本清单](../sources.batch-003.json)、[全部候选](../sources.candidates.json)、[待处理队列](../sources.next.json)。

累计 `raw_path`、`repository_manifest` 相对于 `data/cumulative/`；`collection_root` 指向实际来源目录，清单内部原文路径相对于该来源目录。合并不搬迁原文。原有 `.gitignore` 已覆盖新增批次的原文、Git 缓存及临时文件，无需修改。

## 执行与验证

使用原有 GitHub 联网执行环境，对每个来源执行等价的固定版本采集命令；实际命令和退出码在 `source-results.json`。例如：

```bash
python3 collect_skills.py --self-test
python3 collect_skills.py --sources sources.batch-003.json --only cloudflare/security-audit-skill --out data-batch-003/repositories/cloudflare/security-audit-skill
python3 merge_collections.py --out data-batch-003 --status data-batch-003/source-results.json data-batch-003/repositories/*/*
python3 merge_collections.py --out data/cumulative --status data-batch-002/source-results.json --status data-batch-003/source-results.json data data-batch-002/repositories/*/* data-batch-003/repositories/*/*
python3 tests/verify_collection.py --sources data/cumulative/sources.lock.json --out data/cumulative
```

已有自检通过，24 个采集命令及合并、核验命令均退出码 0。累计核验覆盖全部 **64,708 个 Git 树条目**和 **60,984 个已保存文本文件、581,652,371 字节**，正文和关联文本均与对应固定 Git blob 逐字节一致，合并后原文及清单路径可用。

以 `/tmp/atlas-batch003-initial-verification.json` 保存第一次累计核验基线，实际重新采集固定 commit 的 cloudflare 来源，复用本地 Git 对象，再次合并并执行：

```bash
python3 tests/verify_collection.py --sources data/cumulative/sources.lock.json --out data/cumulative --baseline-report /tmp/atlas-batch003-initial-verification.json > data/cumulative/verification-report.json
```

核验通过：剔除采集与报告时间戳后，索引、文件清单及报告一致，未新增重复记录，也未抹掉其他成功结果。另逐项确认 10,914 条旧记录的字段与指向未丢失；`git diff --check`、原文与缓存忽略规则检查通过。本轮未改变程序，因此未新增测试。

## 下一批继续

从未扫描的队列继续一个来源，使用新的持久输出目录：

```bash
python3 collect_skills.py --sources sources.next.json --only degausai/wonda --out data-batch-004/repositories/degausai/wonda
```

候选首次解析的完整 commit 会写入该输出目录 `sources.lock.json`；同一版本重跑时，将 `--sources` 指向该 lock 文件以复用现有 Git 对象。不要将来源链接或零入口结果计为已保存正文。随后累计：

```bash
python3 merge_collections.py --out data/cumulative --status data-batch-002/source-results.json --status data-batch-003/source-results.json data data-batch-002/repositories/*/* data-batch-003/repositories/*/* data-batch-004/repositories/*/*
python3 tests/verify_collection.py --sources data/cumulative/sources.lock.json --out data/cumulative
```

失败或无入口检查该来源报告，其他来源继续，已成功目录不覆盖。本轮仅扩大可取回、可追溯的原始材料，未进入内容处理阶段，也未声称覆盖整个 GitHub。
