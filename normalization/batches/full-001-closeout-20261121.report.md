# full-001 标准化续跑检查点

截至 2026-11-21 23:52（北京时间），55,703 条可读入口中累计保存 **51,455 条** SkillRecord，确认重复并关联到已保存记录 **4,218 条**，两者合计覆盖 **99.95%**。另有 99 条未导出入口在可读队列之外。

此次续跑前已保存 51,446 条，剩 39 条。30 条是已记录的服务端内容过滤拒绝，按既有规则保留。9 条因模型写错证据路径未通过检查。本机 Ollama 的 `qwen3-vl:8b-instruct-q4_K_M` 补跑这 9 条，新增保存 **9 条**。证据路径在本地请求的输出约束中限定为实际输入路径，记录继续使用 SkillRecord v0.1.1 和原检查器。截至该检查点，仍剩 **30 条**，包括 30 条内容过滤、0 条其他失败，以及 0 条待处理。

本地模型返回 10 次生成响应，报告 70,489 个输入 token 和 32,881 个输出 token。本地模型清单摘要为 `70e58e37beb15464e19aa903dedb2cba828b89c0fb186f75ff2738c7347b96eb`。原请求、响应和完整记录保存在本机。

补跑前用 `python3 normalization/run_corpus.py --snapshot 20261121-path-retry` 对已有结果核验来源、结构、实际模型响应和重复关联。补跑后逐条核验新增结果的固定来源、供给范围、行号、原响应一致性与 SHA-256。`python3 normalization/run_tests.py` 的 87 项回归和 1,000 组区间对照通过，`python3 -m unittest discover -s normalization -p 'test_*.py'` 的 87 项测试通过。结果维持未完整语义审阅状态。

| 采集 ID | 本机结果路径（相对于仓库根目录） | SHA-256 |
| --- | --- | --- |
| `4557ba35004e05c81cdd` | `data/normalized/full-001/batches/1665/records/9dac73b17dce7ce128646b1d4beb500b.json` | `d6bea2f5755505a74b61bcaefc9c814fa117317acf0787cb957a255312f97a40` |
| `47f9ef8ba487bfe867a5` | `data/normalized/full-001/batches/1665/records/d3b745bf787ec5659c69e9a954ff80cd.json` | `c8c136797e1cd114a648d5eeb541411806c42a79e4c78f828bb76e71b856d9fd` |
| `74a8a9984cc492848271` | `data/normalized/full-001/batches/1139/records/38ddc8d208d27a1e89b52e2fdc681bea.json` | `fd73142c717b742a5e048326ef46970478b98cc0a3ceaf1e8c39328399413409` |
| `87827cec9592f43e0ece` | `data/normalized/full-001/batches/1767/records/3f7e8c8b3c95477b23feaff7080d4e24.json` | `31613ae5ab61dc67fdcd6a0bfc334fdad3316b5fdd776eda2fcbc6053e140fe0` |
| `88d5e19f5501a1d053f4` | `data/normalized/full-001/batches/1898/records/28213385b73e694d4eb0c4dac9dcbfdb.json` | `eb8595e7a6800dafd25bc91eae25ff515b14655889c5a7bad48c47a8e07d9b64` |
| `c2fa1ccda3f33f8ce892` | `data/normalized/full-001/batches/2076/records/5da45dc6d89d6c4cf9c6d344e07cae43.json` | `ca2252a69e2084537fc605b1de32b87678676160111ac5df1fa1a19f02137163` |
| `c91b55718a5970a098ea` | `data/normalized/full-001/batches/1895/records/0690db89ead0bcdd8cfe1bc2fc105225.json` | `a5352fd08a3c0b07807a484466fbb11453dd3bda03c5626f821ea94148409dcc` |
| `dbec1fdc1c5768f993b4` | `data/normalized/full-001/batches/1898/records/88890b3c807d56bcbf8c18df574ec9c0.json` | `7ab95104da3874ea9f6b9e3218ae1bce37490a211e6d8a6605aefcb7d6b3e373` |
| `ffc8fbb4d717f341de1d` | `data/normalized/full-001/batches/2100/records/4fbfe16175e000ac541f69a1e4e66a96.json` | `acbf2134b65b46c6a6aa1f265c7484948d823de0d10541bed25b65edeb093ab9` |
