"""铜币拍卖模块（spec §14 / plan §18）。

与 `app/fetcher.py` / `app/scorer.py` 同属业务层：不依赖 FastAPI、不感知请求上下文，
因此 `python -m app.cli coins-*` 能跑通与调度器完全相同的管道。

子模块：
- `profile`：`orders.xlsx` → 结构化画像标签
- `gapi`：麦稀奇小程序网关（专场 + 拍品，免登录、免浏览器）
- `mxiqi`：麦稀奇网页（仅成交价搜索，要登录、要浏览器）
- `matcher`：画像匹配
- `pricing`：站内历史成交查询 + 推荐价统计
- `pipeline`：采集 → 匹配 → 定价 编排
"""
