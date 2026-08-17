# 轻量级 Web 预约系统（100% 免费版）

给新西兰 Auckland / Browns Bay 本地小店（理发店、钢琴老师、私教）用的扫码预约系统。
**目标：跑起来不花一分钱，也不需要信用卡。**

---

## 免费清单（为什么免费）

| 用途 | 选型 | 费用 |
|---|---|---|
| 后端框架 | FastAPI（开源） | 免费 |
| 数据库 | SQLite（Python 自带） | 免费 |
| 前端框架 | Vue 3（已本地打包 `static/vendor/`，无需 CDN、无需构建、可离线） | 免费 |
| 网页托管 | Render 免费版（或 PythonAnywhere 免费版） | 免费 |
| 二维码 | Python `qrcode` 库本地生成 | 免费 |
| 顾客提醒 | 确认页「加到我的日历」(ICS) —— 用顾客自己的手机日历提醒 | 免费 |
| 老板提醒 | 自家邮箱 SMTP（如 Gmail 应用专用密码） | 免费 |
| 日历同步 | Google Calendar API（OAuth，免费） | 免费 |
| AI 解析 | Google Gemini（`gemini-flash-latest` 免费 Flash 层级，无需信用卡） | 免费 |
| AI 兜底 | 未配置密钥时内置**本地规则解析**，同样能把示例短信解析入库 | 免费 |

> ⚠️ 唯一通常会花钱的是**短信(SMS)提醒**。我们不用它：顾客确认后把预约加进自己的手机日历，手机到点免费提醒；老板侧用免费邮件提醒。这样 No-show 逻辑依然成立，且零成本。

> 🛠️ 已修复的两个实用坑：① 顾客页日期默认用**浏览器本地时区**（Auckland），不会再出现「今天显示成昨天、看不到空档」的问题；② Vue 已**本地打包**，断网或被墙也能正常打开页面。

> 🏪 **多租户（Multi-tenancy）**：一个平台可以开很多家店，数据完全隔离。平台方用**超级管理员**账号在 `/super-admin.html` 创建小店和老板账号；每家店有专属预约链接 `/book/{slug}`；老板用各自的用户名/密码登录 `/admin.html`。密码用 bcrypt 哈希存储，登录用 JWT，所有查询强制按 `shop_id` 隔离。

---

## 本地运行（3 步）

```bash
cd booking-system
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 可以先不改，留空也能跑
python main.py              # 打开 http://localhost:8000
```

- 顾客页：`http://localhost:8000/`（输入店铺码，例如 `demo`）或 `http://localhost:8000/book/demo`
- 老板后台：`http://localhost:8000/admin.html`（用**老板用户名 + 密码**登录）
- 超级管理员控制台：`http://localhost:8000/super-admin.html`（用 `.env` 里的 `SUPER_ADMIN_PASSWORD` 登录，默认 `super123`，部署前改掉）
- 演示店铺已自动种入：老板用户名 `admin` / 密码 `admin123`，专属链接 `/book/demo`

> 首次运行若数据库为空，会自动创建一家「Demo Barber」（slug=`demo`）和老板账号 `admin/admin123`，方便你立刻体验。

---

## 免费部署到 Render（给别人用）

1. 把 `booking-system` 推到你的 GitHub 仓库。
2. 在 [render.com](https://render.com) 新建 **Web Service**（免费），连上仓库。
3. Build Command：`pip install -r requirements.txt`
4. Start Command：`uvicorn main:app --host 0.0.0.0 --port 10000`
5. 在 Render 的 Environment 里加 `SUPER_ADMIN_PASSWORD`（超级管理员密码）、`SECRET_KEY`（随机长字符串，`openssl rand -hex 32`）、`PUBLIC_URL`（你的 `.onrender.com` 地址）。
6. 部署完成后，用超级管理员登录 `/super-admin.html` 创建小店，把生成的专属预约链接二维码打印贴店里。

> Render 免费版在无访问时会休眠，第一次打开稍慢，属正常。

---

## 用户流程（最短路径）

扫码 → 选服务 → 选时间（冲突自动置灰）→ 填**姓名 + 邮箱** → 确认 → 成功页点「加到我的日历」（或加 Google 日历）。

> 顾客端**不再要求填手机号**（避免校验出错）；改为只需姓名 + 邮箱，确认邮件和免费提醒都发到这个邮箱。

## 老板后台 MVP

- 当天排班视图（时间 / 服务 / **顾客姓名** / 邮箱 / 状态）
- 🕒 **可预约时间段设置**：按周一~周日分别设置营业起止时间（某天留空/勾选 Closed 即当天休息）+ 时段粒度（如 30/60 分钟）。顾客端只会出现你设置的可约时间。
- 📅 **日历导出 / iCal 订阅**：一键把排班导出 `.ics`（可导入任意日历），或复制一条**私密订阅链接**，让 Google / Apple / Outlook 自动同步你的排班。
- 服务管理（增删、时长、价格）
- 预约状态修改（完成 / 失约）
- 用户名 + 密码登录（bcrypt 哈希 + JWT）
- ✨ **AI 一键记账 / 排班**：粘贴短信或笔记，AI 自动提取并入库（见下）

---

## ✨ AI 智能解析（老板的省事利器）

老板不用逐项手填。在后台顶部粘贴一段短信 / 笔记，点「AI 解析并保存」，系统会：

1. 用 **Google Gemini**（`gemini-flash-latest` 免费 Flash 层级，始终指向当前最新的免费 Flash 模型）把文本解析成结构化数据：
   `customer_name`、`phone_number`（+64 格式）、`service_name`、`price`、`booking_time_local`（奥克兰本地时间，自动处理夏令时）。
2. 把相对时间（"tomorrow"、"Tuesday"）按 `Pacific/Auckland` 解析，**转换成 UTC** 存入数据库（状态默认 `confirmed`）。
3. 自动匹配已有服务；匹配不到就新建一个。

**零配置也能用**：只要没填 `GEMINI_API_KEY`，后端自动用内置的本地规则解析（示例文本照样能解析入库）。所以你可以**先跑通流程**，再决定要不要去申请免费 Key。

**申请免费 Key（可选，无需信用卡）**：
1. 打开 [aistudio.google.com/apikey](https://aistudio.google.com/apikey)，用你的 Google 账号登录。
2. 生成 API Key，复制。
3. 在 `.env` 里填 `GEMINI_API_KEY=你的key`（注意：用 **AI Studio** 的 Key，不是 Vertex，这样才不用绑卡）。
4. 重启服务即可生效。

> 接口：`POST /api/admin/ai-parse-booking`，请求体 `{"raw_text": "老板粘贴的文本"}`。

---

## 多租户：平台方与超级管理员

一套系统服务很多家店，数据彼此隔离。三个角色：

| 角色 | 登录入口 | 密码来源 | 能做什么 |
|---|---|---|---|
| **超级管理员**（平台方 / 你） | `/super-admin.html` | `.env` 的 `SUPER_ADMIN_PASSWORD` | 创建小店 + 老板账号、查看所有店铺与专属链接 |
| **小店老板** | `/admin.html` | 超级管理员创建时生成 | 看本店排班、管本店服务、用 AI 记账 |
| **顾客** | `/book/{slug}` | 无需登录 | 选服务、选时间、预约 |

**创建一家新店（3 步）**
1. 用 `SUPER_ADMIN_PASSWORD` 登录 `/super-admin.html`。
2. 填「店铺名 / 店主用户名 / 初始密码」，点「Create shop」。
3. 系统返回专属链接 `/book/{slug}` 和老板账号；把链接二维码贴店里，把账号交给老板。

**数据隔离怎么保证**：每家店一条 `shops` 记录，老板账号在 `users` 表（`role='shop_owner'`，绑定 `shop_id`）。登录后拿到 JWT（内含 `shop_id`）；后端所有查询都强制 `WHERE shop_id = 当前用户.shop_id`，跨店访问直接 404/403，不会泄露别人家的数据。密码用 **bcrypt** 加盐哈希，绝不存明文。

> 接口：`POST /api/super-admin/login`、`POST /api/super-admin/create-shop`、`GET /api/super-admin/shops`。

## No-show 逻辑（免费实现）

- 后台每 30 秒检查：到点 +15 分钟宽限后仍 `pending` → 自动标 `no_show`。
- **预约成功即发确认邮件**给顾客（发到他留的邮箱；需填 SMTP，没填也能预约，只是不发邮件）。
- **T-24h 自动提醒**：到点前 24 小时，同时给**顾客**和**老板**发一封免费提醒邮件（需填 SMTP）。
- 顾客也可在成功页把预约加进自己的手机日历，靠手机自带提醒。
- 时区统一存 UTC，按 `Pacific/Auckland` 显示，自动处理夏令时。

---

## 📅 日历导出 / iCal 订阅（免费，三端通用）

老板后台有两个入口：

1. **Download .ics**：把本店全部排班导出成一个 `.ics` 文件，双击即可导入 Apple 日历 / Outlook；或上传到 Google Calendar。
2. **订阅链接（Subscription link）**：一条带密令的 URL（`/api/book/{slug}/calendar.ics?t=...`）。把它贴进 Google Calendar（「通过 URL 添加」）、Apple 日历（「新建订阅日历」）或 Outlook，之后你的排班会**自动同步**，每次新增/改期都不用再手动导入。

> 安全：订阅链接里带一个用 `SECRET_KEY` 算出的 HMAC 密令，不知道这条完整链接的人无法访问，所以不会泄露你的顾客信息。部署前务必把 `.env` 的 `SECRET_KEY` 改成随机长字符串。

顾客成功页也提供两个按钮：**「Add to Apple / Outlook calendar」**（下载该次预约的 `.ics`）和 **「Add to Google Calendar」**（一键跳转到 Google 添加）。

> 接口：`GET /api/admin/export-ics`（需老板 JWT）、`GET /api/book/{slug}/calendar.ics?t=密令`（订阅源）、`GET /api/book/{slug}/booking/{id}/ics`（单次预约）。

## 可选：接入 Google Calendar（免费）

1. Google Cloud Console 建项目 → 启用 **Calendar API**。
2. 配置 OAuth 同意屏幕（External / Testing），加你的邮箱为测试用户。
3. 用 OAuth 拿到 `access_token`，存成 JSON，设置 `GOOGLE_TOKEN_FILE` 指向它。
4. 在 `.env` 设 `GOOGLE_CALENDAR_ENABLED=true`。
5. 之后每次预约确认会自动在你的日历建一个事件（免费）。

> 注意：OAuth 应用保持 **Testing 模式**即可（最多 100 个测试用户，无需谷歌审核），足够自用和几家店。

---

## 给 13 岁开发者的提醒

- 付费服务（如真要发短信）要用**父母的账户并征得同意**，别自己绑卡。
- 后台密码、邮箱密码属于私密信息，**不要**提交到公开的 GitHub。
- 时区是第一号 bug 源：本系统已统一存 UTC、按奥克兰显示，别手写时差。
- 先跑通「扫码预约 → 后台看到排班」，再接日历和邮件。
