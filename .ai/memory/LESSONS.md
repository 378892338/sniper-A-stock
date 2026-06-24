# 经验教训库 (Lessons Learned)

> 自动维护。格式: `## [LESSON-NNN] [类型] 简述`
> 类型: 纠错型 | 发现型 | 模式型 | 决策型
> 状态: ACTIVE | SUPERSEDED | NEEDS_REWRITE

---

## [LESSON-001] [发现型] Python 3.14 + EastMoney IIS TLS 互操作不兼容
- **创建**: 2026-06-23
- **触发场景**: Python 3.14 的 `urllib.request` 和 `requests`+`urllib3` 2.7.0 均无法连接 EastMoney `push2his.eastmoney.com`（IIS/10.0），SSL 握手阶段 `RemoteDisconnected`。
- **根因**: CPython 3.14 的 SSL context 初始化参数与 EastMoney IIS 服务器的 TLS 栈不兼容（ALPN/TLS 扩展协商失败）。raw socket + `ssl.create_default_context().wrap_socket()` 可正常握手。
- **正确做法**: `data/sources/eastmoney.py` 的 `_fetch_kline` 改用 raw socket + `ssl.SSLContext` 手动发送 HTTP/1.1 GET 请求，绕过 `urllib.request`。**必须开启证书验证和主机名验证**（`create_default_context()` 默认行为），禁止 `check_hostname=False`。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-002] [发现型] Baostock 服务端 IP 黑名单封禁
- **创建**: 2026-06-23
- **触发场景**: Baostock `bs.login()` 返回 `error_code=10001011, error_msg=网络接收错误`，所有子进程 `BSLOGIN_FAIL`。此前 `data/local/updater.py` 用 subprocess 并发 333 个 baostock worker，每个重试 login 3 次（15s），导致管道卡死数小时。
- **根因**: Baostock 服务端对频繁请求的 IP 实施黑名单封禁。
- **正确做法**: `_baostock_worker.py` 已标记 DEPRECATED。个股日线改为 `_fetch_failed_with_fetcher()` 多源自动降级链（jqdata→akshare→sina）。**不要在 baostock 不可用时反复重试**。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-003] [模式型] 批量数据获取前必须做源健康预探测
- **创建**: 2026-06-23
- **触发场景**: 批量下载 4235 只股票时，前几只股票每个故障源浪费 ~56s（15s 超时 × 3 次重试），多线程同时卡住。新增 `_probe_source_health()` 后，1 只股票预探测所有源，故障源全局标记跳过，后续批量阶段零超时浪费。
- **正确做法**: 在 `_fetch_failed_with_fetcher()` 开头调用 `_probe_source_health("600436", start, end)`，使用代表性股票遍历 `DATA_SOURCE_PREFERENCE`，失败源自动被 `HealthTracker` 标记不可用。批量阶段 `Fetcher` 直接跳过不可用源。
- **效能评分**: 1/1
- **状态**: ACTIVE

## [LESSON-004] [决策型] 日报管道不应因数据源故障而终止
- **创建**: 2026-06-23
- **触发场景**: `run_pipeline.py` 的 `_run_single_day()` 中，当 `_wait_for_data()` 返回 False 时直接 `return False` 退出，导致即使有历史数据也无法生成日报。2026-06-23 日报因此未生成。
- **正确做法**: 数据源故障时改为 `logger.warning` + 继续执行后续步骤（信号刷新→L2预计算→策略→日报→Obsidian）。现有数据仍可生成有价值的日报。
- **效能评分**: 1/1
- **状态**: ACTIVE

## [LESSON-005] [纠错型] 外部代码审查可能包含完全虚构的类名和架构
- **创建**: 2026-06-23
- **触发场景**: 多次收到声称"审查通过"的代码修改建议，引用了不存在的类名（`TencentDirectDataSource`, `NetworkManager`, `CacheWriterThread`, `TradingCalendar` 等），以及与当前代码完全无关的架构描述（连接池、单写多读模型、跨源融合等）。
- **根因**: AI 生成的代码审查基于推测而非实际代码。必须用探测脚本核实每一条声明。
- **正确做法**: 在接受任何外部代码审查前，用 `grep`/`Glob` 逐条验证审查中声称存在的类名、函数名、文件名。不接受未经验证的架构描述。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-006] [模式型] HTTP 响应解析必须基于 Content-Length 而非 Connection: close
- **创建**: 2026-06-23
- **触发场景**: raw socket HTTP 实现中，初版仅靠 `Connection: close` 后 socket 关闭来判断 body 结束。这在 HTTP/1.1 keep-alive 或服务器不主动关闭连接的场景下会导致 recv 永久阻塞。
- **正确做法**: 解析 HTTP 响应头中的 `Content-Length`，精确控制 body 读取范围。`Content-Length` 缺失时才 fallback 到读至 socket close。**必须加总超时 deadline 守卫**（`time.time() + 30s`），防止网络分区导致 recv 永久阻塞。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-007] [纠错型] COUNT 覆盖率阈值 + INSERT OR IGNORE 导致 _wait_for_data 死循环
- **创建**: 2026-06-23
- **触发场景**: `_verify_daily_bars_coverage` 要求最新日期股票数 ≥3000。批量下载 2924 只，但 1897 只是 `INSERT OR IGNORE`（数据已存在），实际新写入仅 903 只。903 < 3000 → 判定不达标 → 触发第二轮 `weekly_update.main()` → 死循环。
- **根因**: COUNT 阈值无法区分"数据真的缺失"和"数据已存在无需写入"。INSERT OR IGNORE 被算失败但 SQLite 数据正确。
- **正确做法**: 删除 `MIN_COVERAGE` 阈值，只验证 `MAX(date) >= today`。COUNT 阈值仅在防御 baostock `mark_updated` 空写时有意义，baostock 弃用后无价值。
- **效能评分**: 1/1
- **状态**: ACTIVE

## [LESSON-008] [发现型] Claude Code Bash hook 默认解析 stdout 为 JSON 导致频繁报错
- **创建**: 2026-06-23
- **触发场景**: `PreToolUse:Bash hook error — Hook JSON output validation failed — (root): Invalid input` 频繁弹出。hook 脚本用 `echo` 输出纯文本日志，框架默认尝试解析 stdout 为 JSON 失败。
- **根因**: `settings.json` 中 hook 未声明 `"output": "text"`，框架按默认 JSON 模式解析所有 stdout 输出。
- **正确做法**: 所有非 JSON 输出的 hook 必须加 `"output": "text"`。或脚本层面将所有日志重定向到 stderr（`>&2`），stdout 保持空。
- **效能评分**: 1/1
- **状态**: ACTIVE

## [LESSON-009] [纠错型] 日报"可入场"候选未排除已持仓股票导致逻辑矛盾
- **创建**: 2026-06-24
- **触发场景**: 日报中 300401 同时出现在"止损已触发 建议离场"和"可入场"两个互斥状态中。根因是 `entry_pass` 过滤只检查了 `soft_min_score` 阈值，未检查 `positions` 是否已持有。
- **正确做法**: 构建 `entry_pass` 时用 `held = set(positions.keys())` 过滤，`if c["symbol"] in held: continue`。`collect_l2` 产生的 `candidates` 是纯评分排序，必须与 `positions` 交叉排除后再展示。
- **效能评分**: 1/1
- **状态**: ACTIVE

## [LESSON-010] [决策型] 盘中初稿不能复用 generate_html — BacktestEngine.run() 会意外开仓
- **创建**: 2026-06-24
- **触发场景**: 14:45 盘中初稿直接用 `generate_html()` → `BacktestEngine.run(date,date)` 用盘中价建仓，违反 T+1。改跑 `engine.run(yesterday)` 又遇隔离失真。
- **正确做法**: 独立 `generate_intraday_html()`，不调引擎。持仓从 `position_snapshot.parquet`（16:00 管道写）读，市价从 daily_bars 最新 close 推算。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-011] [纠错型] PID 文件锁必须心跳防系统休眠僵死
- **创建**: 2026-06-24
- **触发场景**: 12:00 管道启动 → 系统休眠 → 进程挂起 → 16:00 查锁 PID 仍存在 → 跳过正式版。
- **正确做法**: 锁文件含 PID+start_time。每 60s 更新 mtime（os.utime）。检查时若心跳>180s → 僵死 → 覆盖锁。Windows 用 `tasklist /FI "PID eq N"` 替代 kill -0。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-012] [发现型] mootdx Quotes.factory(heartbeat=True) 在 Windows 下导致 TCP 连接卡死
- **创建**: 2026-06-24
- **触发场景**: 编写 mootdx.py 时 `Quotes.factory(heartbeat=True, bestip=True)` 导致实例创建永久阻塞。改为 `heartbeat=False, bestip=False` 后立即响应。
- **正确做法**: `Quotes.factory(market='std', timeout=15, heartbeat=False, bestip=False)`。RateLimiter 0.3s 限流 + @retry(2) 足够覆盖偶发 TCP 抖动。
- **效能评分**: 0/0
- **状态**: ACTIVE

## [LESSON-013] [模式型] 外部 API 阻断时用 _disabled 标记 + register 但不激活的隔离模式
- **创建**: 2026-06-24
- **触发场景**: eastmoney_fundflow.py / eastmoney_dt.py 在 API 阻断期无法端到端测试，但代码已完成。`is_available()` 直接返回 False，仍注册到工厂字典。
- **正确做法**: 数据源类注册到 _sources 但不参与降级链。API 恢复后只需改 is_available() 逻辑即可激活。不破坏现有降级链，不留 syntax error 残留。
- **效能评分**: 0/0
- **状态**: ACTIVE
