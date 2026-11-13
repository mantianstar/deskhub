status: done

- 已完成：
  - 确认需求：把 `app.cli` 的 `coins-grade` 拆成两个命令——① 视觉模型只取「特征」；② 推理模型按特征出「品相」。不再走「一张图一次出六行」的单步流程。
  - 已定位现有链路（沿用 `2026-11-12-coin-grade-flash.md` 的结论）：`app/cli.py` 命令入口 → `app/coins/best.py` 评级候选与日额度 → `app/coin_grade/mimo_client.py`（OpenAI 兼容多模态客户端，图片 data URL）→ `app/coin_grade/prompt.py`（固定六行提示词）→ `app/coin_grade/parse.py`（六行解析）。
  - 配置位：`config/config.yaml` 第 25-38 行 `coin_grade` 段（`model: mimo-v2.6-pro`、base_url `https://api.xiaomimimo.com/v1`、temperature 0.1、timeout 120）；`app/config.py` 的 `CoinGradeConfig`。
  - 交接 `2026-11-12-coin-grade-flash.md` 已置 `status: done`（该文档是探索性质，未产出可落地结论）。
- 未完成：
  - 未改任何代码：两个命令、两段提示词、两套解析、特征中间产物的存放都还没动。
  - 未确认 `app/cli.py` 里 `coins-grade` 的现有参数（币种筛选、数量上限、是否 dry-run、日额度计数位置）。
  - 未确认特征中间产物放哪里（DB 新表 / 现表加列 / JSON 文件），以及 `app/coins/best.py` 的日额度 `repository.py:1531 count_coin_graded_on_day` 是按命令计还是按品相结果计。
  - 未确认是否存在依赖旧 `coins-grade` 一体命令的定时任务、页面或测试。
- 下一步：
  1. 读 `app/cli.py`（先 `^(class|def) ` 出符号表，再按行号读 `coins-grade` 命令体），确认参数与调用点。
  2. 读 `app/coin_grade/{prompt,parse,mimo_client}.py`，把「特征」拆成可复用结构（币面读数：年份、造币厂标记、磨损点、清洗/腐蚀迹象、边齿与材质描述等），特征提示词只要求结构化字段，不出档位与分数。
  3. 设计两步衔接：特征写入中间产物 → `coins-grade` 只读特征（不再送图）调推理模型出六行，`parse.py` 保持六行输出不变以兼容下游。
  4. 配置加两个模型：`coin_grade.vision_model`（取特征）与 `coin_grade.grade_model`（推理出品相），保留 `coin_grade.model` 兼容或直接迁移。
  5. 补测试：特征解析用例、品相解析用例、CLI 两命令冒烟；跑 `pytest -q tests/test_coin_grade.py`。
- 约束：
  - 不改 `llm` 段，不影响其它 LLM 调用路径。
  - 品相侧最终输出仍是六行（`app/coin_grade/parse.py` 与下游依赖），只有中间特征格式是新增的。
  - 两命令都要支持按币种筛选与数量上限，且日额度口径不因拆分而翻倍。
- 读取起点：
  - `app/cli.py`（搜 `coins-grade`）
  - `app/coin_grade/prompt.py`、`app/coin_grade/parse.py`、`app/coin_grade/mimo_client.py`
  - `app/coins/best.py`、`app/repository.py:1531`
  - `config/config.yaml` 第 25-38 行
  - `tests/test_coin_grade.py`
