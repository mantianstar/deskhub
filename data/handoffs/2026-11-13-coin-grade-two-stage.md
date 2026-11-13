status: done

# 铜币品相评级改两阶段（2026-11-13）

- 已完成：
  - `app/coin_grade/prompt.py` 换成两阶段提示词（`VISION_SYSTEM_PROMPT` / `build_vision_user_prompt`、`GRADER_SYSTEM_PROMPT` / `build_grader_user_prompt`），口径照 `data/handoffs/2026-11-11-coin-grade-mimo-prompt.md`。
  - `app/coin_grade/mimo_client.py` 增加纯文本 `complete()`，保留 `MimoClient` 类名。
  - `config/config.yaml` 的 `coin_grade` 拆为 `vision`（qwen3-vl-plus / DESKHUB_QW_VL_API_KEY）与 `reason`（deepseek-v4-flash / DESKHUB_LLM_API_KEY）；`app/config.py` 相应改为 `CoinGradeStageConfig` + 两段解析。
  - 新增 `coin_grade_features` 表（`app/models.py`，SCHEMA_VERSION 38；`app/db.py` 加 ("37","38")；`app/migrations/__init__.py` 补 20–36 直升 38 的沿用项）；`app/repository.py` 新增 `save_coin_grade_features`，待评级队列带出已存特征。
  - `app/coins/best.py` 的 `grade_candidates` 与 `app/cli.py` 的 `coins-grade` 均改为「视觉提特征并落库 → 推理评级」。
  - `app/coin_grade/parse.py` 适配七行输出（新增「瑕疵与盲区」→ blind_spots）。
  - 测试与 CHANGELOG 已更新。
- 未完成：无。
- 下一步：真实环境跑一次 `coins-grade` 前需在 `.env` 配 `DESKHUB_QW_VL_API_KEY`（DashScope）与 `DESKHUB_LLM_API_KEY`。
- 约束：评级口径不得放宽；不臆测图外信息；旧 `data/handoffs/2026-11-12-coin-grade-flash.md` 按用户要求未采用、未改动。
- 读取起点：`app/coin_grade/prompt.py`、`app/coins/best.py#grade_candidates`、`app/repository.py#save_coin_grade_features`。
