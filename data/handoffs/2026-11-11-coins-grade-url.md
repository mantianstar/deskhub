status: done（2026-11-12 用户确认已修，无需续跑）

# coins-grade 传 URL 报「图片不存在」

## 已完成
- 定位报错点：`app/coin_grade/mimo_client.py:88-90`，`_user_content` 对每张图做 `Path(raw_path).is_file()` 校验，失败抛 `MimoError(f"图片不存在：{path}")`。
- 定位 CLI 参数：`app/cli.py:744-752`，`coins-grade` 的 `obverse` / `reverse` 只声明为「正面图路径 / 背面图路径」，没有 URL 处理。
- 结论（根因）：命令只支持本地文件，不支持 http(s) URL；`Path("https://oss...")` 归一化会把 `//` 折叠成 `/`，所以报错文本显示 `https:/oss...`。图片本身没问题，是入参形态不支持。

## 未完成
- 未修改任何代码（本轮只做只读排查，未出任务卡）。
- 未验证该 OSS 链接是否可匿名下载（未发网络请求）。

## 下一步
- 待用户确认要哪种方案后再出任务卡（S 类）：
  1. 最小改：`_user_content` 前先判断 `raw_path` 以 `http://` / `https://` 开头则直接用作 image_url，不做 `Path` 校验（改 `app/coin_grade/mimo_client.py`，补 `tests/test_coin_grade.py` 用例）。
  2. CLI 层：`coins-grade` 前先下载到临时目录再走本地路径（新增下载逻辑，改动面更大）。
  3. 不改代码：让用户先 `curl` 下载到本地再传路径。

## 约束
- 不改既有文案与测试断言；报错文案 `图片不存在：{path}` 保留。
- 不改 `Path` 折叠行为导致的展示差异（除非方案 1 落地，届时 URL 分支不再进 `Path`）。

## 读取起点
- `app/coin_grade/mimo_client.py` 第 82-96 行（`_user_content`）
- `app/cli.py` 第 398-420 行（`cmd_coins_grade`）、第 744-752 行（参数定义）
- `tests/test_coin_grade.py`
