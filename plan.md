# AI 个人工作台 — 技术方案（索引）

> 本目录只写**「现在怎么做」**（状态快照）；「为什么这么改」见 [CHANGELOG.md](./CHANGELOG.md)。
> 需求见 [spec.md](./spec.md)；执行任务与验证见 [tasks.md](./tasks.md)。

## 分册

| 文件 | 内容 | 章节 |
|---|---|---|
| [plan/core.md](./plan/core.md) | **通用**：选型 / 架构 / 目录 / 配置 / 数据层 / 调度运维 / 与 spec 差异 / 测试策略 / 风险 / 运行控制 | §1~§6、§11~§13、§16、§17、§19 |
| [plan/knowledge.md](./plan/knowledge.md) | **知识聚合**：抓取 / 打分 / 日报 / 页面 / 里程碑 / 源清单 | §7~§10、§14、§15 |
| [plan/coins.md](./plan/coins.md) | **铜币**：拍卖管道 / 参考库 / 信息页面 / 图片缓存 / 品相录入 | §18（含 §18.1~§18.16） |

> **编号全局唯一**，跨文件引用写全路径，如 `plan/coins.md §18.7`；文中只写 `§6.2` 时按下表定位。
> 凡是与 spec 冲突的地方，在 [plan/core.md](./plan/core.md) §12 集中列出并说明理由。

## 阅读方式

- 做**知识聚合** → 读 [plan/knowledge.md](./plan/knowledge.md)，通用约束按需查 [plan/core.md](./plan/core.md)。
- 做**铜币** → 读 [plan/coins.md](./plan/coins.md)。
- 做**运行控制 / 调度 / 数据层 / 测试** → [plan/core.md](./plan/core.md) 对应章节。
