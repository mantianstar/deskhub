status: pending

- 已完成：
  - 定位现网配置：`config/config.yaml` 第 33-38 行 `coin_grade.model = mimo-v2.6-pro`（base_url `https://api.xiaomimimo.com/v1`，temperature 0.1，timeout 120）。
  - 定位调用链：`app/config.py`（CoinGradeConfig，第 112 / 264 / 443 行）→ `app/coin_grade/mimo_client.py`（独立 OpenAI 兼容多模态客户端，图片走 data URL）→ `app/coin_grade/prompt.py`（固定六行输出）→ `app/coin_grade/parse.py`；批量入口 `app/coins/best.py` 的评级候选流程，日额度见 `app/repository.py:1531 count_coin_graded_on_day`。
  - 历史背景：CHANGELOG v1.156 / 交接 `2026-11-11-coin-grade-mimo.md`、`2026-11-12-coins-best.md`。
- 未完成：
  - 未取得 mimo-v2.6-pro 与 mimo-v2.6-flash 的官方单价（本地仓库无价格表），因此「省多少」没有可落地的数字。
  - 未实测单次评级的 token 用量（输入 = 系统提示 + 系列字段 + 2 张图；输出 = 六行），缺实测基线。
  - 未评估换 flash 对评级质量（XF/UNC 档位稳定性、分数抖动）的影响。
- 下一步：
  1. 取单价：查小米 mimo 官方定价页，记 pro / flash 的输入（含图像）与输出每百万 token 价格。
  2. 测基线：包一层 `app/coin_grade/mimo_client.py` 记录每次请求 usage（当前是否回传 usage 需确认），用同 10-20 枚币各跑 pro/flash 各一次。
  3. 出三个数：单次成本、日额度 50 条下的日/月成本、逐枚档位一致率与分数差（MAE）。
  4. 结论按「成本降幅 ≥ 80% 且档位一致率 ≥ 95% 才建议切」的口径给建议；否则只做可配置（`coin_grade.model` 已可改，无需改代码）。
- 约束：
  - 只动 `coin_grade` 段配置，不改 `llm` 段，不影响其它 LLM 调用路径。
  - 不改既有提示词与六行输出格式（`app/coin_grade/parse.py` 依赖）。
- 读取起点：
  - `config/config.yaml` 第 25-38 行
  - `app/coin_grade/mimo_client.py`
  - `app/coin_grade/prompt.py`
  - `app/coins/best.py`
