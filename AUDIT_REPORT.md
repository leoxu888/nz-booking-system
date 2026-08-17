# nz-booking-system 代码审计与上市就绪度评估

审计日期：2026-08-17
审计范围：`main.py` / `database.py` / `auth_utils.py` / `emailer.py` / `calendar_sync.py` / `static/*` / 测试套件
审计方式：全量代码走读 + 运行现有测试 + 编写对抗性测试实际复现（真实 uvicorn 服务器 + 并发压测）

---

## 一、总体结论

**结论：目前不适合上市（不适合开始向商家收费）。**

先说好的一面，这些是真实的工程亮点，不是客套话：

- **多租户隔离的核心逻辑是正确的。** 所有 `/api/admin/*` 查询都强制 `WHERE shop_id = <JWT 里的 shop_id>`。我专门尝试了跨店越权（A 店老板改 B 店预约 / 删 B 店服务 / 读 B 店排班），全部正确返回 404/403。这一点很多商业项目都做错了。
- **时区处理是这个项目最漂亮的地方。** 统一存 UTC、按 `Pacific/Auckland` 展示，而且循环预约特意避开了 "aware 时间 + timedelta(weeks)" 这个跨夏令时会漂移 1 小时的经典陷阱（`main.py:745-756` 的注释说明作者真的想清楚了）。这个坑绝大多数开发者第一次都会踩。
- 密码 bcrypt 加盐、JWT 鉴权、CSV 注入防护（`main.py:1205-1208`）、Vue 本地打包避免 CDN 依赖、前端无 `v-html`（无 XSS 面）——这些都是有意识的安全设计。
- `test_multitenant.py` 73 项检查全部通过，覆盖了隔离、鉴权、营业时间、blackout、容量、循环预约、老顾客识别。**功能层面的"正常路径"是通的。**

但是：**这个系统当前在"正常路径"之外会坏，而且坏在最要命的地方——它会把同一个时段卖给多个顾客，会向任何陌生人泄露全部顾客的姓名/邮箱/手机号，并且在部署时只要漏配一个环境变量就会被完全接管。** 这三件事任意一件发生在真实商家身上，都足以让你失去这个客户并承担新西兰《Privacy Act 2020》下的通报义务。

一句话定位：**这是一个完成度很高的 MVP / 作品集项目，但还不是一个可以收钱的商业产品。** 距离可以做付费试点，大约需要 2-4 周的集中修复（先修 P0 + P1）。

---

## 二、P0 阻塞问题（上线前必须修，已实际复现）

### P0-1 并发下会重复售卖同一时段（最严重）

**复现结果：69 个时段中有 31 个被卖给了多个顾客，最多 4 个人被塞进同一个 30 分钟档期。**

```
rounds: 69 x 24 concurrent same-slot POSTs, 8 live workers
total bookings: 123   slots sold MORE THAN ONCE: 31
  !! 4x at 2026-09-18T21:00:00+00:00: R1,R3,R0,R2
  !! 4x at 2026-09-20T21:00:00+00:00: R6,R4,R1,R3
  !! 3x at 2026-09-21T21:00:00+00:00: R0,R6,R5
```

**根因**：`main.py:757` 调用 `_slot_free()` 做检查，`main.py:762` 才 INSERT。`_slot_free()` → `generate_slots()` 用的是**另一个独立数据库连接**（`database.py:32`），检查和写入之间没有任何原子性保证；数据库层也没有 `UNIQUE(shop_id, start_utc)` 约束兜底。两个请求可以同时通过检查，然后都写入成功。

> 注：单 worker 跑的时候很难复现（SQLite 的粗粒度文件锁碰巧把它串行化了），这也是现有测试没抓到它的原因。一旦多 worker 或迁移到 Postgres，它就是必然事件。

**修复**：
```sql
CREATE UNIQUE INDEX ux_booking_slot ON bookings(shop_id, start_utc)
  WHERE status IN ('pending','confirmed');
```
再把"检查 + 写入"包进单个事务（`BEGIN IMMEDIATE`），并捕获 `IntegrityError` 返回 409。约束是唯一可靠的防线，应用层检查只是用来给出友好提示。

---

### P0-2 任何陌生人都能拖走一家店的全部顾客个人信息

**复现结果**：`GET /api/book/{slug}/booking/{id}/ics` **完全不需要认证**，HTTP 200，返回内容里包含 `jane.smith@realmail.co.nz` 和 `+64211234567`。

**根因**：`main.py:807-829`。这个端点只校验 `slug`（店铺 slug 是公开的，就印在店里的二维码上）和 `bid`，而 `bid` 是自增整数。攻击者扫一遍 `bid=1,2,3...` 就能导出该店**所有顾客的姓名 + 邮箱 + 手机号**。

同类问题：`main.py:848-869` 的 `/manage/{token}` 用的是 128 位随机 token，这个是安全的；但 `/booking/{bid}/ics` 用的是可枚举的自增 ID，把前者的安全设计完全绕过了。

**这在新西兰《Privacy Act 2020》下属于需要向 OPC 通报的隐私事件（notifiable privacy breach）。**

**修复**：ICS 下载必须凭 `manage_token` 访问（`/api/book/{slug}/manage/{token}/ics`），或改用不可枚举的 UUID 作为预约对外标识。

---

### P0-3 漏配 `SECRET_KEY` = 零凭据接管整个平台

**复现结果**（模拟运维只忘了设 `SECRET_KEY`、超管密码设得很强的真实部署）：

```
SECRET_KEY actually in use: 'dev-insecure-secret-change-me-in-production'
=== ATTACKER knows nothing but the public source code ===
1) forge super_admin -> GET /api/super-admin/shops : HTTP 200
   platform-wide shop list: [('demo','admin'), ('browns-bay-barber','boss')]
2) forge super_admin -> create shop           : HTTP 200
3) forge shop_owner  -> read tenant bookings   : HTTP 200
   stolen record: Jane Smith / jane@realmail.co.nz / +64211234567 @ 2026-08-26 09:00
4) forge iCal token  -> GET calendar.ics       : HTTP 200, PII leaked=True
5) forge shop_owner  -> export full CSV        : HTTP 200, rows=1
```

**根因**：`auth_utils.py:13` 的兜底值 `"dev-insecure-secret-change-me-in-production"` 是写在源码里的公开字符串。只要环境变量没设，任何人都能用这个字符串签一个 `{"role":"super_admin"}` 的 JWT，或者签一个任意 `shop_id` 的 `shop_owner` JWT。**超管密码设得多强都无关——攻击者根本不走登录。**

雪上加霜的两点：
1. **没有任何启动守卫**。程序不会因为用了默认密钥而拒绝启动，甚至不打印警告。Render 上漏配一个环境变量太容易了。
2. `main.py:925` 的 `calendar_token()` 用了**另一个**硬编码兜底值 `"dev-insecure-secret-change-me-local-only"`（和 `auth_utils.py` 不一致），所以订阅链接也是可伪造的。而且这个 token 是 `HMAC(key, slug)`，**永不轮换**——链接一旦泄露，除了改全局 `SECRET_KEY`（会同时踢掉所有店的登录态和所有订阅链接）之外无法吊销。

**修复**：
```python
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY must be set to a random 32+ char value")
```
删掉所有硬编码兜底值。订阅 token 改为每店独立、存库、可单独轮换。

---

### P0-4 多 worker 启动就崩，被迫单进程运行

**复现结果**：`uvicorn main:app --workers 8` → 8 个 worker 里 7 个在启动阶段挂掉：

```
sqlite3.IntegrityError: UNIQUE constraint failed: shops.slug   (x7)
```

**根因**：`main.py:1431-1452` 的 `seed_demo_shop()` 是"先 SELECT 判断有没有店，再 INSERT"，在 lifespan 启动阶段执行。多 worker 会并发跑同一段逻辑，全部通过检查，然后抢着插入 `slug='demo'`。

**影响**：你现在只能单 worker 运行。单进程 = 无法利用多核、一个慢请求（比如卡住的 SMTP）就阻塞全站。这同时也是 P0-1 在生产环境被暂时"掩盖"的原因。

**修复**：`INSERT OR IGNORE`，或把建表/种子数据挪到独立的启动脚本 / migration 步骤，不要放在每个 worker 的 lifespan 里。

---

### P0-5 SQLite + Render 免费版 = 数据必然丢失

Render 免费版容器的文件系统是**临时的**：每次重新部署、重启、休眠唤醒都可能重置磁盘。`booking.db` 就在应用目录里（`database.py:16`），意味着**所有店铺、老板账号、顾客预约会在某次部署后凭空消失**，而且没有任何备份机制、没有 migration 机制。

README 第 50-59 行推荐的正是这条部署路径。**照着 README 部署给真实商家用，就是在等一次彻底的数据丢失。**

（说明：这一条是 Render 平台的已知特性，我没有实际在 Render 上验证；但 SQLite 文件位于容器内这一点是代码事实。）

**修复**：上线必须换 Postgres（Render/Supabase/Neon 都有免费额度），并接入定时备份。同时补上 `PRAGMA foreign_keys=ON`、WAL、`busy_timeout`——目前这三项全部缺失（实测 `foreign_keys=0`、`journal_mode=delete`）。

---

## 三、P1 严重功能与业务缺陷（已实际复现）

### P1-1 几乎每一个顾客预约最终都会被自动标成"失约"

**复现结果**：一条正常的 `pending` 预约，在到点 15 分钟后跑一次调度，状态变成 `no_show`。

**根因**：顾客下单写入的状态是 `'pending'`（`main.py:767`），而**系统里没有任何逻辑把 `pending` 升级为 `confirmed`**。`run_reminders()`（`main.py:1495-1497`）则会把所有超过开始时间 15 分钟的 `pending` 一律标成 `no_show`。

**后果**：理发师只要忘了在 15 分钟内点一下"完成"，**已经到店并付了钱的顾客就被记录成失约**。`/api/admin/stats` 报出来的 no-show 率完全是噪音，而这恰恰是 README 主推的卖点之一。

**修复**：下单即写 `confirmed`（顾客自助下单本身就是确认行为）；自动 `no_show` 前要求人工复核，或至少延长宽限期并只对真正未处理的预约生效。

---

### P1-2 T-24h 提醒对 `confirmed` 预约永不触发

**复现结果**：一条 5 小时后开始的 `confirmed` 预约，跑完调度 `reminded_24h` 仍为 `0`。

**根因**：`main.py:1491` 的查询写死了 `WHERE b.status = 'pending'`。而 AI 记账创建的预约默认就是 `confirmed`（`main.py:639`），老板手动确认过的预约也是 `confirmed`——**这些预约永远收不到提醒邮件**。修完 P1-1 之后，这个 bug 会导致提醒功能对所有预约彻底失效。

**修复**：改为 `status IN ('pending','confirmed')`。另外改期后应重置 `reminded_24h`（目前不重置，改期的顾客收不到新提醒）。

---

### P1-3 顾客姓名可以往老板日历里注入伪造日程（iCalendar 注入）

**复现结果**：把顾客姓名填成含 CRLF 的字符串下单，老板导出的 `.ics` 里出现了一个我伪造的日程 `SUMMARY:INJECTED ALL-DAY BLOCK`。

**根因**：`main.py:267-286` 的 `ics_event()` 把 `service_name` / `name` / `email` / `phone` 直接拼进 ICS 行，**完全没有 RFC 5545 的转义和折行处理**。

**后果**：恶意顾客可以往老板订阅的日历里塞任意事件（比如整天的"店铺关闭"），或者直接把整个订阅源搞坏。即使没有恶意，一个名字里带 `,` `;` `\` 的顾客（在新西兰完全正常）也会让日历订阅解析失败。

**修复**：实现标准转义（`\` → `\\`、`;` → `\;`、`,` → `\,`、换行 → `\n`），并按 75 字节折行。

---

### P1-4 老板能一键把自己的预约页搞成永久 500

**复现结果**：`PATCH /api/admin/shop` 提交 `"2": ["25:00","26:00"]` → **接受，HTTP 200**；随后访问该店周三的可约时段 → `ValueError: hour must be in 0..23`。

**根因**：`main.py:1033-1038` 只校验了 `opening_hours` "是不是合法 JSON"，完全没校验内容。`database.py:105-110` 的 `_hhmm()` 原样返回 25，`main.py:210` 用它构造 `datetime(..., 25, 0)` 直接抛异常。

**后果**：老板一个手误（把 `15:00` 打成 `25:00`）就会让自家预约页对所有顾客 500，而且**他自己无法恢复**——必须有人去手改数据库。对付费商家来说这是灾难级的支持事件。

**修复**：校验 `0 <= 时 <= 23`、`0 <= 分 <= 59`、结构必须是 7 天完整映射，非法直接 400。

---

### P1-5 删除服务会静默"吞掉"已有预约，并把档期二次售卖

**复现结果**：给某服务建一条预约 → 删掉该服务 → 该预约在后台列表/ICS/统计里**全部消失**，但数据库里那行还在；同时那个时间段**被重新开放给新顾客**。

**根因**：`main.py:1287` 直接 `DELETE FROM services`，不检查是否有预约引用。所有查询都是 `bookings JOIN services`，服务没了 JOIN 就查不出来。外键虽然在 `database.py` 里声明了，但**从未启用**（实测 `PRAGMA foreign_keys = 0`），所以是装饰性的。

**后果**：一个真实顾客的预约凭空消失，老板不知情，那个时段又被卖给了别人。

**修复**：启用 `PRAGMA foreign_keys=ON`；服务改为软删除（`archived` 标记），有预约引用时禁止硬删除。

---

### P1-6 AI 记账完全绕过所有可用性校验

**复现结果**：
- 该店当天已被设为 blackout（休假关店）→ AI 记账照样创建成功（HTTP 200）
- 同一个下午 3 点连续记两笔 → **两笔都写入，同一时刻 2 条预约**
- 记一个**已经过去**的时间（今天凌晨 1 点）→ HTTP 200，接受

**根因**：`save_ai_booking()`（`main.py:616-654`）从头到尾没有调用 `_slot_free()`，不检查冲突、不检查营业时间、不检查 blackout、不检查容量、不检查是否在过去。

**修复**：AI 写入走和顾客下单同一条校验路径；冲突时返回给老板确认，而不是静默写入。

---

### P1-7 无 Gemini key 时的本地兜底解析会静默编造错误数据

**复现结果**：

| 输入文本 | 解析出的时间 | 解析出的姓名 |
|---|---|---|
| `Haircut for Zoe on 2026-09-30 at 15:00 $30` | **2026-08-17**T15:00（今天！） | **"Haircut"** |
| `Sarah 25/09 2pm haircut $30` | **2026-08-17**T14:00（今天！） | Sarah |
| `Mike next Friday 10am trim $25` | 2026-08-21T10:00 ✓ | Mike |

**根因**：
- `_resolve_date()`（`main.py:483-496`）只认 `today` / `tomorrow` / 英文星期名，**任何明确日期格式（`2026-09-30`、`25/09`）都被忽略，静默 fallback 到"今天"**。
- `_resolve_name()`（`main.py:550-558`）取第一个首字母大写且不在停用词表里的单词——所以 `"Haircut for Zoe"` 里顾客名成了 `"Haircut"`。

**后果**：README 把"零配置也能用"当作卖点（第 90 行），但这条路径会**静默产生日期错误的预约**，没有任何警告。老板以为记好了 9 月 30 日的单，实际记在了今天。

**修复**：兜底解析解不出明确字段时应**拒绝并要求人工确认**，不要猜。补上常见日期格式的解析。

---

### P1-8 服务名模糊匹配会挂上错误的时长和价格

**复现结果**：店里同时有 `Haircut`（30 分钟 / $30）和 `Cut`（120 分钟 / $200），请求匹配 `"Cut"` → 实际返回 `Haircut`。

**根因**：`main.py:605` 用的是 `rn == tl or rn in tl or tl in rn` 这种朴素子串匹配。`"cut" in "haircut"` 为真，两个服务互相吞并，谁先建谁赢。

**后果**：预约挂上错误的时长（影响后续档期计算）和错误的价格（直接影响收入）。

**修复**：改为精确匹配 + 显式别名表，或用带阈值的模糊匹配并把结果交给老板确认。

---

### P1-9 多租户在"通知"和"日历"两条链路上是假的

这是**架构层面**的缺口，不是小 bug：

**复现结果**：`shops` 表的列是 `['id','name','slug','business_hours_start','business_hours_end','slot_minutes','created_at','opening_hours','daily_capacity']`——**没有任何一列存店主邮箱**。

- `main.py:777`、`888`、`1502` 全都把通知发给 `os.getenv("SHOP_EMAIL")`——**一个全局环境变量**。所以所有店铺的新预约通知都发到同一个邮箱（平台方的），**每个店主都收不到自己店的通知**。README 第 18 行承诺的"老板侧用免费邮件提醒"实际不成立。
- `calendar_sync.py:21` 的 `push_event()` 用一个全局 `GOOGLE_TOKEN_FILE` 和 `calendar_id="primary"`。一旦 `GOOGLE_CALENDAR_ENABLED=true`，**所有租户的顾客预约都会写进同一个 Google 日历**（平台方的）——这本身就是跨租户数据泄露。
- 同一个文件里的 access_token 来自静态 JSON、**没有 refresh 逻辑**。Google access token 1 小时过期，之后 `push_event()` 静默返回 `None`。README 第 144-152 行描述的 Google 日历集成实际上只能工作一小时。

**修复**：`shops` 表加 `owner_email`、`google_refresh_token`、`google_calendar_id`，所有通知和日历写入按 `shop_id` 路由。

---

### P1-10 没有任何速率限制，公开端点可被轻易滥用

**复现结果**：
- 25 次连续错误密码登录 → 状态码全是 `[401]`，**从未出现 429**，无锁定、无退避。唯一的减速手段是 bcrypt 本身。
- **一个匿名 POST**（`repeat_weeks: 52`）→ HTTP 200，**一次抢占 52 个档期**，并在同一个请求里同步触发 **104 封 SMTP 发信**。

邮箱地址**从不做验证**，也没有验证码。任何人都能：把一家店未来一年的档期用假名字填满（业务层面的拒绝服务）；或者拿别人的邮箱地址下单，把你的服务器当成邮件轰炸中继（进而让你的 SMTP 域名被拉黑）。

**修复**：登录端点加速率限制 + 失败锁定；下单加验证码或邮箱验证（先发确认链接再落库）；`repeat_weeks` 上限降到合理值（如 12）并要求验证后的邮箱；邮件改为后台队列异步发送。

---

### P1-11 SMTP 同步阻塞且无超时

`emailer.py:23` 的 `smtplib.SMTP(host, port)` **没有传 timeout**，而它是在 HTTP 请求线程里同步调用的（`main.py:776`）。一个卡住的邮件服务器会一直挂住 worker——而由于 P0-4，你只有一个 worker。

同时没有重试和队列：确认邮件发失败就永久丢失，而**那封邮件里的 manage 链接是顾客唯一能取消/改期的入口**。

**修复**：`smtplib.SMTP(host, port, timeout=10)`，邮件发送移出请求路径（后台任务/队列），失败要重试并记录。

---

## 四、P2 工程质量与合规缺口

**测试**
- `test_smoke.py` 已经**完全失效**——它调用的是早已删除的单租户接口（`/api/services`、`/api/qr`、`/api/admin/today`），第 20 行直接 `KeyError: 0` 崩掉。应删除或重写。
- `test_multitenant.py` 73 项全过，但把日期**硬编码**在 2026-09（`date=2026-09-10` 等）。过了那个月，这些日期变成过去时间，`generate_slots()` 会过滤掉它们，测试会集体失败。应改用相对日期。
- 同一个文件注释说"故意不设置 GEMINI_API_KEY → 走本地兜底解析，结果 deterministic"，但 `main.py:28` 的 `load_dotenv()` 会把 `.env` 里的**真实 key 读进来**（`load_dotenv()` 是相对 `main.py` 所在目录查找的）。所以这个测试实际上在**调用线上 Gemini API**，既不确定也在消耗配额。

**账号运维（对商业化是硬伤）**
- 没有任何修改密码 / 找回密码的接口。店主忘记密码只能由你手工改数据库。
- `users.username` 是**全局唯一**（`database.py:134`），两家店不能都有叫 `admin` 的店主。开到第几家店就会撞。
- 每店只支持一个账号，无法给员工开子账号。
- 创建店铺时明文回显初始密码（`main.py:1403`）。
- JWT 有效期 7 天且**无法吊销**，没有 logout 服务端失效机制。店主手机丢了没法处理。

**性能 / 可扩展性**
- `admin_bookings()`（`main.py:1119`）把该店**全部历史预约**捞进内存再用 Python 过滤，无分页；"老顾客"标记那段逻辑是每行 O(n)、整体 O(n²)。
- 除主键和唯一约束外**没有任何索引**，`bookings(shop_id, start_utc)` 上没有索引。
- `occupied_intervals()` 同样是全表捞出再在 Python 里按日期过滤。
- 小店用一年（几千行）之后，后台会明显变慢。

**其它**
- `requirements.txt` **零版本锁定**。上游任何 breaking release 都会让下次部署静默损坏。至少要 `pip freeze` 出一份锁定版本。
- `get_conn()` 的调用全都不在 `try/finally` 里，任何异常都会泄漏连接句柄。
- `super_admin_login`（`main.py:1361`）用 `!=` 比较密码，非常数时间；应用 `hmac.compare_digest`。
- `zoneinfo` 在部分平台需要 `tzdata` 包，`requirements.txt` 里没有。

**合规（面向新西兰真实商家的硬门槛）**
系统处理的是新西兰居民的姓名、邮箱、手机号，你作为平台方在《Privacy Act 2020》下属于 "agency"，但目前：

- 没有隐私政策，下单页没有任何授权/同意勾选
- 没有数据保留策略，顾客无法要求查看或删除自己的数据（IPP 6 / IPP 7）
- PII 明文存储，无静态加密，无访问审计日志（IPP 5）
- 没有数据泄露响应流程（而 P0-2 就是一个现成的泄露口）
- 没有服务条款
- **没有任何计费/订阅模块**——即使技术上没问题，现在也没有向商家收钱的手段

**当前仓库里的密钥状态**
`.gitignore` 正确排除了 `.env`（做得对）。但 `~/Downloads/nz-booking-system/.env` 里目前是：一个**真实可用的 Gemini API key**、`SUPER_ADMIN_PASSWORD=super123`、`SECRET_KEY=dev-secret-change-me-local-only-0001`（弱且可猜）。
建议：**轮换那个 Gemini key**（Downloads 目录是低信任位置，容易被同步或误分享），生产环境的三个值全部用 `openssl rand -hex 32` 级别的强随机值。

---

## 五、修复优先级建议

**第 1 周 — 止血（不修完不要给任何真实商家用）**
1. `UNIQUE(shop_id, start_utc)` 约束 + 事务化下单（P0-1）
2. ICS 端点改用 `manage_token` 鉴权（P0-2）
3. `SECRET_KEY` 启动强校验，删除所有硬编码兜底（P0-3）
4. `seed_demo_shop` 改 `INSERT OR IGNORE`（P0-4）
5. 下单状态改 `confirmed` + 自动 no-show 逻辑收紧（P1-1）
6. `opening_hours` 内容校验（P1-4）

**第 2 周 — 数据不能丢 + 逻辑正确**
7. 迁移到 Postgres + 定时备份 + migration 机制（P0-5）
8. ICS 转义与折行（P1-3）
9. 服务软删除 + 启用外键（P1-5）
10. 提醒逻辑覆盖 `confirmed`（P1-2）
11. AI 写入走统一校验路径；兜底解析解不出就报错而不是猜（P1-6、P1-7、P1-8）

**第 3-4 周 — 能当产品卖**
12. `shops.owner_email` + 通知/日历按店路由（P1-9）
13. 速率限制 + 邮箱验证 + 邮件异步队列（P1-10、P1-11）
14. 改密/找回密码、用户名按店唯一（P2）
15. 分页 + 索引（P2）
16. 隐私政策、同意勾选、数据删除接口、服务条款（P2 合规）
17. 重写测试套件（相对日期 + 隔离掉真实 API key），补上并发和越权的回归测试

**之后**才是计费、多员工、短信等增量功能。

---

## 六、给作者的一句话

从代码质量判断，写这个项目的人已经理解了多租户隔离、JWT、bcrypt、UTC 存储和夏令时陷阱——这个水平远超"能跑起来"的门槛，README 里那句"给 13 岁开发者的提醒"如果是实情，那这份代码相当了不起。

这次找出的问题，绝大多数不是"不会写"，而是**"还没被真实并发和真实恶意用户教育过"**：检查与写入之间的原子性、可枚举 ID 的越权、默认密钥的启动守卫、用户输入进入 ICS/CSV 之前的转义——这些正是从"能跑的项目"跨到"能卖的产品"之间那道坎。把上面 P0 和 P1 修完，这个东西是真的可以拿去给 Browns Bay 的理发店用的。

建议路径：**先修完 P0 + P1，然后找 1-2 家愿意当小白鼠的熟人店铺免费试点 1-2 个月**（真实数据、真实并发、真实误操作），把试点里暴露的问题修完，再谈收费和推广。
