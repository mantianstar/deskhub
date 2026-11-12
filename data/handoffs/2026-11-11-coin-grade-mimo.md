status: done

- 已完成：
  - 需求判型为 S（新增独立 CLI + 评级模块，不动既有大模型调用），已出四行任务卡。
  - 已从 PROJECT_MAP 定位候选落点：`app/llm.py`（既有 LLM 调用）、`utils/llm_proxy/llm_proxy.py`、`app/cli.py`（命令行入口）、`config/config.yaml` / `.env.example`（模型配置）、`tests/test_llm.py`（测试惯例）、`app/coins/grades.py`（既有品相口径，评级结果可能与它对齐）。

- 未完成：
  - 小米 mimo 调用模块（仅本功能使用，其它 LLM 调用不改）。
  - 铜币品相评级 prompt（按用户给的七维度 / 70 分制 / 约束优化）。
  - CLI：参数为 正面图、背面图、series id 共 3 个；series id 需从库中取系列信息作为上下文。
  - 测试用例 + CHANGELOG 一条。

- 下一步：
  1. 读 `app/llm.py`、`utils/llm_proxy/llm_proxy.py`、`app/cli.py` 的 `^(class|def) ` 符号表，确认调用与 argparse 惯例（openai 兼容？）。
  2. 读 `config/config.yaml` 中模型段落与 `.env.example`，确定 mimo 的 base_url / model / key 放哪；新增独立配置键，不覆盖默认模型。
  3. 新增 `utils/coin_grade/`：`mimo_client.py`（仅此一个客户端）+ `prompt.py` + `cli.py`；图片以 base64 或多模态 URL 提交，series id 走 repository 取系列信息（如暂不便，先留 TODO 并从 Numista 系列表读）。
  4. 新增 `tests/test_coin_grade.py`，只测 prompt 组装与参数解析，不发真实网络请求。
  5. CHANGELOG 记一条。

- 约束：
  - mimo 仅用于本功能，其他大模型调用路径（`app/llm.py`、`utils/llm_proxy/`）保持原样。
  - 只做被要求的：不改既有文案与测试断言，不做无关重命名 / 重构，不改动既有 LLM 客户端行为。
  - 输出必须包含：综合评级（分数 + 中文品相 + 铜色等级）、七维度逐项依据、流通磨损与腐蚀/清洗/人为损伤分开、边缘与清洗状态在证据不足时写“无法判定 / 无法确认”、置信度；疑似青铜病需警示。
  - 单次请求输出 ≤200 行 / 8KB；`Read` 单段 ≤120 行。

- 读取起点：
  - `PROJECT_MAP.md` §0（已读 1-120 行）
  - `app/llm.py`、`utils/llm_proxy/llm_proxy.py`、`app/cli.py`
  - `config/config.yaml`（模型段）、`.env.example`
  - `tests/test_llm.py`、`app/coins/grades.py`（只取需要的片段）
