# 再采集一轮：第 018—025 批报告

日期：2026-11-06（Asia/Shanghai）。复用既有采集器、两个原索引及已有 GitHub 搜索渠道。先保存本轮清单，再连续采集，不扩大到分类、方法、摘要、评分、语义去重或网站。

## 实际结果

本轮尝试 **170 个来源**：169 个此前候选表中未有的仓库，以及 1 个原失败来源重试。结果为 **150 个有正文的新来源、19 个固定版本无入口、1 个访问失败**，本轮没有未导出的入口正文。

- 新增保存 **31,358 条 Skill 入口**，同样为 31,358 个仓库 + commit + 路径的版本记录。
- 本轮有 **30,456 种正文**，其中 756 种此前已存在，**净新增不同正文 29,700 种**。
- 累计 **523 个有正文的来源、55,703 条已保存入口、46,057 种正文**。515 个来源入口完整、8 个历史来源部分保存；累计无入口 31、访问失败 1。
- 累计发现入口 55,802 个，保存 55,703 个。历史 99 个未导出入口保留原记录，本轮未追补，也未计入新增保存数。
- 累计完全相同正文 **5,914 组**，涉及 15,560 条记录，超过各组首条的记录为 9,646 条；全部来源保留。
- 本轮 170 个选定来源均已尝试，各批次待处理均为 **0**。全局尚未完成 Git 树扫描的来源仍为 **1**：已尝试但无法访问的 `cbrock84/headcount`。不存在尚未轮到执行的新候选。

| 批次 | 选入 | 入口完整 | 无入口 | 失败 | 保存入口 | 本批待处理 | 检查点全局未扫描 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 018 | 24 | 21 | 2 | 1 | 905 | 0 | 147 |
| 019 | 24 | 22 | 2 | 0 | 1,607 | 0 | 123 |
| 020 | 24 | 22 | 2 | 0 | 5,280 | 0 | 99 |
| 021 | 24 | 19 | 5 | 0 | 644 | 0 | 75 |
| 022 | 24 | 22 | 2 | 0 | 1,353 | 0 | 51 |
| 023 | 24 | 19 | 5 | 0 | 20,490 | 0 | 27 |
| 024 | 24 | 23 | 1 | 0 | 1,077 | 0 | 3 |
| 025 | 2 | 2 | 0 | 0 | 2 | 0 | 1 |

表中“本批待处理”只统计该批尚未得到处理结果的来源；失败另计。最后一列是每批完成时的全局检查点，不是本批待处理。最终 `sources.next.json` 和 `sources.retry.json` 是同一个失败来源，不能相加。

## 发现与固定范围

原有两个索引的 HEAD 未变，仍为：

- VoltAgent/awesome-agent-skills：`6d483e29da745a010d88974ef6d0e50d15755e9a`。
- devanasolver/awesome-agent-skills：`e7e8e2e75b317c9573019f270aebfd8756a0a419`。

复用 GitHub 仓库搜索 `agent skills` 和 `SKILL.md` 各第一页 100 个结果，按仓库地址去重并排除原有候选后得到 **169 个新候选**。未设置 Star 门槛，也未以索引分类为 ATLAS 分类。搜索命中不直接算 Skill；随后固定完整 commit，扫描隐藏、插件和多层目录中的实际 SKILL.md，再保存文本。

[search.json](../data-batch-018/discovery/search.json) 保留搜索命中和原失败来源 API 查询结果，[index-checks.json](../data-batch-018/discovery/index-checks.json) 保留原索引版本核对。候选总数由 387 增至 **556**。本轮范围在开始采集前冻结，运行中没有继续追加候选。既有成功来源没有刷新版本。

## 访问失败与无入口

`cbrock84/headcount` 的 API 查询仍返回 HTTP 404，实际 Git 重试仍失败：

```text
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

未能解析 commit、扫描文件树或保存正文，没有推断仓库已删除，也未替换为其他来源。历史第 007 批结果保留，本轮第 018 批结果另存；累计按仓库合并失败状态，不把重试计作第二个失败仓库。

以下 19 个来源均已固定完整 commit 并扫描文件树，未发现实际 SKILL.md，因此没有正文计入成果：

- `agentskills/agentskills`
- `heilcheng/awesome-agent-skills`
- `libukai/awesome-agent-skills`
- `NousResearch/hermes-agent-self-evolution`
- `lsdefine/GenericAgent`
- `0xNyk/awesome-hermes-agent`
- `futantan/agent-skills.md`
- `EverMind-AI/SkillCorpus`
- `bergside/design-md-chrome`
- `vibeforge1111/vibeship-spawner-skills`
- `rohitg00/awesome-claude-design`
- `wilwaldon/Claude-Code-Frontend-Design-Toolkit`
- `iannuttall/dotagents`
- `royalpinto007/awesome-agent-skills`
- `v1k22/skillhub-registry`
- `WenyuChiou/ai-research-skills`
- `heyabhishekbajpai/SkillIssue`
- `ithiria894/awesome-claude-code-workflows`
- `HunterisLive-1/mobile-maya-skills`

## 真实保存问题与最小修复

第 020 批在累计合并前被原有哈希检查阻止：`LeoYeAI/agent-master-skills` 的 9 对 Git 文件名分别为 `SKILL.md` / `skill.md`。Mac 的大小写不敏感文件系统将每对写成同一个文件，8 条不同正文被覆盖；另 1 对正文原本相同。

修复限定为文件保存和定位：

- 仅对大小写或 Unicode 规范化冲突路径，保存到同一原文快照下 `.atlas-path-collisions/<原路径SHA-256>/<原文件名>` 的独立位置。
- `skill_path` / 清单 `path` 仍保留原始 Git 路径，`raw_path` 指向实际文件，`storage_path_reason` 说明映射原因。非冲突路径保持原样。
- 正文不改写，frontmatter 从实际保存位置读取，关联文件仍通过原路径清单定位。
- 在两个变体都保存后，只清理哈希等于该冲突组上游内容的旧覆盖副本，不删除未识别的用户改动。

使用原固定 commit `8f88f9a0dd5a1075d4f0c7626f9d960844d57575` 重采，18 个入口文件分别保存，3,451 条入口全部通过核验，再继续后续批次。未通过检查的初次输出没有纳入累计索引，也未通过删除来源或修改原文来绕过检查。该来源的固定版本又实际重跑一次，存储路径及内容稳定。

新增回归测试用真实 Git 树构造大小写和 Unicode 规范化冲突，不依赖宿主能否同时创建两个名称，检查两个原始正文、frontmatter、旧覆盖副本修复、重复导出、共享上下文及累计索引的路径和 Git 校验。

## 实际材料与可取回位置

本轮持久保存 **212,735 个文本文件、1,654,022,620 字节**，位于本机项目 `/Users/milin/Documents/ChatGPT/ATLAS-METHODS/`：

```text
data-batch-018/ ... data-batch-025/
  repositories/<owner>/<repo>/raw/<owner>/<repo>/<commit>/
```

直接打开或复制这些目录即可取回，不需要再次从上游下载。以每条记录的 `raw_path` 定位；冲突文件的真实位置由映射字段说明。旧原文目录保持原位。所有 Skill、README、脚本、模板和提示词均作为研究材料，没有安装或执行，也没有将其中的指令当作开发指令。

另有 **4,259 个仓库树条目**按现有策略未导出：3,136 个非 UTF-8 / 二进制、472 个字体、451 个符号链接、187 个超限条目、13 个子模块。每条在来源 `manifests/` 中保留路径、类型、大小、Git blob / gitlink、不可变链接及原因。本轮入口正文均已保存；这些遗漏属于相关上下文或资产。不声明资产或外部依赖完整可执行，许可线索未知时保持未知。

## 累计索引交付格式

完整累计明文索引为 **132,328,531 字节**，超过 GitHub 普通 Git 单文件 100 MiB 限制，见 [GitHub 官方说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。因此只对这个累计索引进行无损 gzip 包装：

- 本机 `data/cumulative/skills.jsonl` **保留、不删除**，已有采集器、合并器和核验脚本继续使用它。
- Git 中改交付 [skills.jsonl.gz](../data/cumulative/skills.jsonl.gz)，**17,652,079 字节**。压缩前与解压后的 SHA-256 完全相同：`54302e491e5930d597eab8ca3b7bd64d821de529bee091ab04b8e1bedf1f9f56`。
- 所有旧记录包含在新压缩索引中；原始 24,444 条记录的字段和路径逐项核对未变。旧批次索引、清单和历史提交保留，不是丢弃旧成果。
- 只是累计交付文件压缩，没有重写材料格式或迁移原文；每来源及每批索引仍是原有 JSONL。`raw_path` / `repository_manifest` 仍相对于累计目录，压缩文件与明文在同一目录。

解压和后续重新打包命令见 README。原文、Git 缓存、临时文件及本机大明文累计索引不提交；只提交无损压缩索引、来源记录、文件清单、报告和必要修复。

## 验证结果

以下均实际执行并通过：

```bash
python3 collect_skills.py --self-test
python3 -m unittest discover -s tests -p 'test_*.py'
python3 collect_skills.py --sources sources.batch-020.json --only LeoYeAI/agent-master-skills --out data-batch-020/repositories/LeoYeAI/agent-master-skills
python3 tests/verify_collection.py --sources data/cumulative/sources.lock.json --out data/cumulative
```

原有自检和 5 项回归测试通过；8 个批次的已有核验器全部通过。实际重采冲突来源后，再运行原合并器，累计索引剔除 `collected_at` / `generated_at` 后的摘要一致，没有重复计数或丢掉成功结果。gzip 解压结果与明文索引逐字节摘要一致。

最终累计核验通过，覆盖 **523 个有正文的来源、55,703 条保存入口、502,549 个文本文件、5,627,260,663 字节**；文件清单完整、正文及关联文本与固定 Git blob 一致，累计路径可用。旧 24,444 条索引记录完整保留。没有重复运行全量历史下载。

现有合并调用继续适用：

```python
from pathlib import Path
from merge_collections import merge
merge([Path('data')] + sorted(Path('.').glob('data-batch-*/repositories/*/*')),
      Path('data/cumulative'), sorted(Path('.').glob('data-batch-*/source-results.json')))
```

合并后按 README 重新打包压缩累计索引，再提交。核验结果见 [verification-report.json](../data/cumulative/verification-report.json)，运行及修复事件见 [queue-run-018.json](../data/queue-run-018.json)。

## 剩余与继续命令

本轮清单已经全部尝试，批内待处理 0，尚未尝试的新候选 0。唯一全局未扫描来源是上面访问失败的仓库，访问恢复后可重试：

```bash
python3 collect_skills.py --sources sources.retry.json --only cbrock84/headcount --out data-batch-018/repositories/cbrock84/headcount
```

之后根据新报告更新该来源的批次结果和全局队列状态，再累计合并、核验及重新打包。历史 99 个未导出入口继续按既有规则记录。本轮完成的是这份冻结清单的采集，不代表已经穷尽 GitHub。
