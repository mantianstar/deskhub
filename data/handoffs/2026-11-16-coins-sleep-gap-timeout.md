status: done

# 交接：铜币采集日志 15 分钟空档定位 + 改进方案（待落地）

- 已完成：定位 deskhub.log L164→L165（07:10:56 → 07:26:32）空档根因 = macOS 整机睡眠，非代码或网关问题。pmset 证据：07:10:57 `Entering Sleep state due to 'Maintenance Sleep'`，07:26:12 `DarkWake ... rtc/SleepService`，07:28:00 才 `FullWake`（HID）。进程连同 asyncio 定时器与 httpx socket 超时被冻结，唤醒后才结束 `asyncio.sleep(3.8)` 并发请求，网络未就绪卡满 20 秒（`REQUEST_TIMEOUT_SECONDS`）→ 07:26:32 跳过专场 338519；07:26:37 起全 200，网关正常。附带确认：报错冒号后空白源于 `GapiError(f"{action} 调用失败：{exc}")` 只取 `str(exc)`，httpx 超时异常字符串为空、类型名丢失。
- 未完成：改进方案三档均未落地，等用户选择——A 仅日志可诊断（gapi 补 `type(exc).__name__`、pipeline warning 加单场耗时）；B = A + `httpx.TransportError` 一次 5 秒退避重试（需补测试）；C 不改。A/B 属 S 类，动手前须先出任务卡。
- 下一步：用户回 A / B / C；选 A/B 则按「gapi.py:583-604 → pipeline.py:325-341 → tests/test_coins_gapi.py（或同名既有测试）」落地，并跑定向 pytest。若要治根因（超出本仓库范围）：用 `caffeinate -i` 包住服务进程，或 `sudo pmset repeat wakeorpoweron` 提前唤醒，或迁常开机器。
- 约束：本轮未改任何代码、配置与日志；未动采集限速参数、四段串行语义、`MISFIRE_GRACE_SECONDS=3600` 与 `coalesce=True`；诊断结论仅基于 data/logs/deskhub.log 与 pmset 记录，未复跑采集。
- 读取起点：app/coins/gapi.py:583-604（`_call` 重试与异常包装）、app/coins/pipeline.py:325-341（限速 + 抓取失败分支）、app/scheduler.py:36-38（DAILY_HOUR / MISFIRE_GRACE_SECONDS）。
