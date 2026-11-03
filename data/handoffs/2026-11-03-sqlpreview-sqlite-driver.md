status: pending
date: 2026-11-03
topic: SQL Preview 扩展 SQLite 驱动缺失（sql.js）

- 已完成：
  - 确认用户环境：Trae CN + 扩展 mehul.sql-preview-0.6.28-universal（无 node_modules，纯打包 out/）。
  - 定位报错来源：`out/extension.js:19010-19016`（OPTIONAL_DRIVERS 中 sqlite → sql.js），
    以及 `out/extension.js:45451-45458` 的 webview 消息 `installOptionalDependency` 分支。
  - 已向用户说明：该扩展的查询结果确实注册在底部 panel（viewsContainers.panel → sqlResultsView, type webview），
    符合「与 Terminal/Problems/Output 并排」的需求。
- 未完成：
  - 尚未确认 `installOptionalDependency` 的实际安装目录（读到 45458 即被预算截断）。
  - 尚未验证在扩展目录手装 sql.js 后 Trae 能否解析（Node 解析应从扩展根找 node_modules）。
- 下一步：
  - 读 `out/extension.js` 中 `installOptionalDependency(` 定义（类方法，约 46k 行后），确认落盘路径与 npm 参数。
  - 按确认结果给用户二选一：点报错卡片上的安装按钮；或手工把 sql.js 装到扩展 node_modules。
  - 验证：新建 SQLite 连接指向 data/*.db，执行 `select 1` 看底部 Results 是否出网格。
- 约束：
  - 不改仓库业务代码；扩展目录在 ~/.trae-cn/extensions 下，写操作需绕过 sandbox 且升级会被覆盖。
  - 不再重复读已确认区间（18930-19040、45440-45500）。
- 读取起点：
  - `~/.trae-cn/extensions/mehul.sql-preview-0.6.28-universal/out/extension.js` 搜索 `installOptionalDependency(`；
  - `~/.trae-cn/extensions/mehul.sql-preview-0.6.28-universal/out/server/Daemon.js` 搜索 `optionalDrivers` / `sql.js`。
