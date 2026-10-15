# AI 个人工作台 — Spec（索引）

> 本目录只写**「现在该是什么样」**（状态快照）；「什么时候改了什么、为什么」见 [CHANGELOG.md](./CHANGELOG.md)。
> 技术方案见 [plan.md](./plan.md)；执行任务见 [tasks.md](./tasks.md)。

## 分册

| 文件 | 内容 | 章节 |
|---|---|---|
| [spec/core.md](./spec/core.md) | **通用**：目标与反目标 / 范围 / 技术栈 / 目录结构 / 调度与运行控制 | §1~§4、§9 |
| [spec/knowledge.md](./spec/knowledge.md) | **知识聚合**：数据模型 / 抓取 / 打分 / 页面 / 验收 / 参数 / 风险 / 开发顺序 | §5~§8、§10~§13 |
| [spec/coins.md](./spec/coins.md) | **铜币**：拍卖 / 参考库（Numista）/ 信息页面 / 图片缓存 | §14（含 §14.1~§14.15） |

> **编号全局唯一**，跨文件引用写全路径，如 `spec/knowledge.md §6`；文中只写 `§6` 时按下表定位。
> 本文件是八轮需求拷问后的收敛结果。凡未在分册中的能力，v1 一律不做。

## 阅读方式

- 做**知识聚合** → 读 [spec/knowledge.md](./spec/knowledge.md)，通用约束按需查 [spec/core.md](./spec/core.md)。
- 做**铜币** → 读 [spec/coins.md](./spec/coins.md)。
- 做**运行控制 / 调度** → [spec/core.md](./spec/core.md) §9，实现见 [plan/core.md](./plan/core.md) §19。
