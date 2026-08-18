"""多租户端到端测试：验证隔离、JWT 鉴权、超级管理员、AI 解析、预约流程。
使用 FastAPI TestClient（直接跑路由，不依赖端口/代理）。
注意：TestClient 必须作为上下文管理器使用，lifespan 才会跑（建表 + 种入 demo 店铺）。
"""
import os
import json
os.environ["DB_PATH"] = "/tmp/mt_test.db"
os.environ["SUPER_ADMIN_PASSWORD"] = "super123"
os.environ["SHOP_TZ"] = "Pacific/Auckland"
os.environ["PUBLIC_URL"] = "http://localhost:8000"
# 强制走 SQLite（项目里的 .env 若含 DATABASE_URL，load_dotenv 会覆盖不到已存在的键）
os.environ["DATABASE_URL"] = ""
# 关闭限流，避免同 IP 的多次登录/下单触发 429 干扰其它断言
os.environ["RATE_LIMIT_DISABLED"] = "1"
# 故意不设置 GEMINI_API_KEY -> 走本地兜底解析，结果 deterministic

import shutil
for f in ("/tmp/mt_test.db",):
    try: shutil.rmtree(f, ignore_errors=True)
    except OSError: pass
    try: os.remove(f)
    except OSError: pass

import main
from fastapi.testclient import TestClient

passed = 0
failed = 0
def check(name, cond):
    global passed, failed
    status = "PASS" if cond else "FAIL"
    if cond: passed += 1
    else: failed += 1
    print(f"[{status}] {name}")

# TestClient 作为上下文管理器 -> lifespan 跑起来（建表 + 种入 demo 店铺 + 启动调度线程）
with TestClient(main.app) as client:

    # ---------- 1. 演示店铺已种入 ----------
    r = client.get("/api/book/demo/shop")
    check("demo 店铺可访问 (slug=demo)", r.status_code == 200 and r.json()["slug"] == "demo")

    # ---------- 2. 超级管理员登录 ----------
    r = client.post("/api/super-admin/login", json={"password": "wrong"})
    check("超级管理员错密码 -> 401", r.status_code == 401)
    r = client.post("/api/super-admin/login", json={"password": "super123"})
    check("超级管理员登录成功", r.status_code == 200)
    super_token = r.json()["token"]

    # ---------- 3. 创建两家店 + 老板账号 ----------
    rA = client.post("/api/super-admin/create-shop",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"shop_name": "Barber A", "owner_username": "bossA", "owner_password": "pwA123"})
    check("创建店铺 A", rA.status_code == 200 and rA.json()["slug"] == "barber-a")
    slugA = rA.json()["slug"]; urlA = rA.json()["booking_url"]
    rB = client.post("/api/super-admin/create-shop",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"shop_name": "Piano B", "owner_username": "bossB", "owner_password": "pwB123"})
    check("创建店铺 B", rB.status_code == 200 and rB.json()["slug"] == "piano-b")
    slugB = rB.json()["slug"]

    rDup = client.post("/api/super-admin/create-shop",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"shop_name": "X", "owner_username": "bossA", "owner_password": "x"})
    check("重复老板用户名 -> 400", rDup.status_code == 400)

    rNoAuth = client.post("/api/super-admin/create-shop",
        json={"shop_name": "X", "owner_username": "zzz", "owner_password": "x"})
    check("未登录创建店铺 -> 401", rNoAuth.status_code == 401)

    # ---------- 4. 老板登录 ----------
    r = client.post("/api/admin/login", json={"username": "bossA", "password": "pwA123"})
    check("老板 A 登录成功", r.status_code == 200)
    tokenA = r.json()["token"]
    r = client.post("/api/admin/login", json={"username": "bossA", "password": "bad"})
    check("老板错密码 -> 401", r.status_code == 401)
    r = client.post("/api/admin/login", json={"username": "bossB", "password": "pwB123"})
    tokenB = r.json()["token"]

    # ---------- 5. 数据隔离：A 的服务 / 预约对 B 不可见 ----------
    r = client.post("/api/admin/services",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"name": "Beard Trim", "duration_min": 30, "price": 35})
    check("老板 A 新增服务 Beard Trim", r.status_code == 200)
    svcA_id = r.json()["id"]

    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-10")
    slots = r.json()
    check("A 店有可约时段", len(slots) > 0)
    slot = slots[0]["start_local"]
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slot,
              "customer_name": "Alice", "customer_email": "alice@example.com"})
    check("顾客在 A 店下单成功 (姓名+邮箱)", r.status_code == 200)
    bookingA_id = r.json()["id"]
    bookingA_token = r.json()["manage_url"].split("/")[-1]
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slot,
              "customer_name": "Bob", "customer_email": "bob@example.com"})
    check("A 店重复时段 -> 409", r.status_code == 409)
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slot, "phone": "64219999999"})
    check("缺姓名/邮箱下单 -> 拒(400/422)", r.status_code in (400, 422))

    r = client.get("/api/admin/services", headers={"Authorization": f"Bearer {tokenB}"})
    check("B 店服务列表不含 A 的服务", all(s["name"] != "Beard Trim" for s in r.json()))
    r = client.get("/api/admin/bookings?date=2026-09-10", headers={"Authorization": f"Bearer {tokenB}"})
    check("B 店排班为空（隔离）", r.status_code == 200 and len(r.json()) == 0)
    r = client.get("/api/admin/bookings?date=2026-09-10", headers={"Authorization": f"Bearer {tokenA}"})
    check("A 店排班含刚下的单", any(b["id"] == bookingA_id for b in r.json()))

    r = client.get(f"/api/book/{slugB}/services")
    check("B 店顾客页无 A 的服务", all(s["name"] != "Beard Trim" for s in r.json()))

    # ---------- 6. 跨店越权：A 改 B 的预约应 404 ----------
    r = client.post("/api/admin/services",
        headers={"Authorization": f"Bearer {tokenB}"},
        json={"name": "Lesson", "duration_min": 45, "price": 40})
    svcB_id = r.json()["id"]
    r = client.get(f"/api/book/{slugB}/availability?service_id={svcB_id}&date=2026-09-10")
    slotB = r.json()[0]["start_local"]
    r = client.post(f"/api/book/{slugB}/bookings",
        json={"service_id": svcB_id, "start_local": slotB,
              "customer_name": "Carol", "customer_email": "carol@example.com"})
    bookingB_id = r.json()["id"]
    r = client.patch(f"/api/admin/bookings/{bookingB_id}",
        headers={"Authorization": f"Bearer {tokenA}"}, json={"status": "done"})
    check("A 越权改 B 预约 -> 404", r.status_code == 404)
    r = client.patch(f"/api/admin/bookings/{bookingB_id}",
        headers={"Authorization": f"Bearer {tokenB}"}, json={"status": "done"})
    check("B 改自己预约 -> 200", r.status_code == 200)
    r = client.delete(f"/api/admin/services/{svcB_id}", headers={"Authorization": f"Bearer {tokenA}"})
    check("A 越权删 B 服务 -> 404", r.status_code == 404)

    # ---------- 7. 鉴权守卫 ----------
    check("无 token 访问 /api/admin/shop -> 401",
          client.get("/api/admin/shop").status_code == 401)
    check("老板 token 访问超管列表 -> 403",
          client.get("/api/super-admin/shops", headers={"Authorization": f"Bearer {tokenA}"}).status_code == 403)

    # ---------- 8. AI 解析（本地兜底，按 shop 隔离） ----------
    r = client.post("/api/admin/ai-parse-booking",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"raw_text": "Kia ora, Tom tomorrow 3pm haircut $30 txt 0211234567"})
    check("A 店 AI 解析成功", r.status_code == 200)
    ai = r.json()
    check("AI 解析提取姓名 Tom", ai.get("customer_name") == "Tom")
    check("AI 解析 +64 手机", ai.get("phone_number") == "+64211234567")
    bA_id2 = ai["booking_id"]
    r = client.get("/api/admin/bookings?date=2026-09-11", headers={"Authorization": f"Bearer {tokenB}"})
    check("AI 预约不泄露到 B 店", not any(b["id"] == bA_id2 for b in r.json()))

    # ---------- 9. ICS / QR ----------
    r = client.get(f"/api/book/{slugA}/booking/{bookingA_id}/ics?token={bookingA_token}")
    check("ICS 下载 200 且为 calendar 类型", r.status_code == 200 and "text/calendar" in r.headers["content-type"])
    r = client.get(f"/api/book/{slugA}/qr")
    check("QR 图片 200 (image/png)", r.status_code == 200 and "image/png" in r.headers["content-type"])

    # ---------- 10. 列表店铺 ----------
    r = client.get("/api/super-admin/shops", headers={"Authorization": f"Bearer {super_token}"})
    shops = r.json()
    check("超管能看到所有店铺(>=3)", r.status_code == 200 and len(shops) >= 3)
    slugs = {s["slug"] for s in shops}
    check("列表含 barber-a / piano-b / demo", {"barber-a","piano-b","demo"} <= slugs)

    # ---------- 11. 自定义营业时间（按天） ----------
    # 构造完整 7 天配置：周一关闭，其余 09:00-18:00，时段 60 分钟
    full_hours = {str(i): (["09:00", "18:00"] if i != 0 else None) for i in range(7)}
    r = client.patch("/api/admin/shop",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"opening_hours": json.dumps(full_hours), "slot_minutes": 60})
    check("老板 A 更新营业时间 -> 200", r.status_code == 200)
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-14")  # 周一
    check("周一设为休息后无时段", r.status_code == 200 and len(r.json()) == 0)
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-15")  # 周二
    check("周二(营业)有时段", r.status_code == 200 and len(r.json()) > 0)
    # 还原为默认全营业、时段 30
    restore = {str(i): ["09:00", "18:00"] for i in range(7)}
    client.patch("/api/admin/shop",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"opening_hours": json.dumps(restore), "slot_minutes": 30})

    # ---------- 11b. 多时段（午休）+ Blackout 日期 ----------
    tu = {str(i): ["09:00", "18:00"] for i in range(7)}
    tu["1"] = [["09:00", "12:00"], ["14:00", "18:00"]]   # 周二：上午+下午，午休
    r = client.patch("/api/admin/shop",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"opening_hours": json.dumps(tu), "slot_minutes": 30})
    check("老板 A 设置多时段营业 -> 200", r.status_code == 200)
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-15")  # 周二
    labs = [s["label"] for s in r.json()]
    check("多时段：上午有时段(09:30)", "09:30" in labs)
    check("多时段：下午有时段(15:00)", "15:00" in labs)
    check("多时段：午休无时段(13:00)", "13:00" not in labs)
    # Blackout：把 2026-09-16(周三) 设为关店
    r = client.post("/api/admin/blackout",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"date": "2026-09-16", "note": "Public holiday"})
    check("添加 Blackout 日期 -> 200", r.status_code == 200)
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-16")
    check("Blackout 当天无时段", r.status_code == 200 and len(r.json()) == 0)
    r = client.delete(f"/api/admin/blackout/2026-09-16",
        headers={"Authorization": f"Bearer {tokenA}"})
    check("删除 Blackout 日期 -> 200", r.status_code == 200)
    # 还原
    client.patch("/api/admin/shop",
        headers={"Authorization": f"Bearer {tokenA}"},
        json={"opening_hours": json.dumps(restore), "slot_minutes": 30})

    # ---------- 11c. 日程范围筛选 + 姓名/邮箱搜索 ----------
    r = client.get("/api/admin/bookings",
        headers={"Authorization": f"Bearer {tokenA}"},
        params={"from_date": "2026-09-10", "to_date": "2026-09-10"})
    check("范围筛选(单日)含 Alice 预约", any(b["id"] == bookingA_id for b in r.json()))
    r = client.get("/api/admin/bookings",
        headers={"Authorization": f"Bearer {tokenA}"}, params={"q": "alice"})
    check("搜索 alice -> 命中 Alice", any(b["email"] == "alice@example.com" for b in r.json()))
    r = client.get("/api/admin/bookings",
        headers={"Authorization": f"Bearer {tokenA}"}, params={"q": "zzznotfound"})
    check("搜索无匹配 -> 空", len(r.json()) == 0)

    # ---------- 11d. 状态变更（通知邮件在 SMTP 未配置时仅打印日志） ----------
    r = client.patch(f"/api/admin/bookings/{bookingA_id}",
        headers={"Authorization": f"Bearer {tokenA}"}, json={"status": "done"})
    check("改状态 done -> 200", r.status_code == 200)
    r = client.get("/api/admin/bookings",
        headers={"Authorization": f"Bearer {tokenA}"},
        params={"from_date": "2026-09-10", "to_date": "2026-09-10"})
    check("状态已更新为 done", any(b["id"] == bookingA_id and b["status"] == "done" for b in r.json()))

    # ---------- 12. 日程导出 .ics + iCal 订阅 ----------
    r = client.get("/api/admin/export-ics", headers={"Authorization": f"Bearer {tokenA}"})
    body = r.text
    check("导出 .ics 200 且含 VCALENDAR/VEVENT",
          r.status_code == 200 and "BEGIN:VCALENDAR" in body and "BEGIN:VEVENT" in body)
    check("导出 .ics 含顾客邮箱(Alice)", "alice@example.com" in body)

    info = client.get("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA}"}).json()
    tok = info["calendar_token"]
    r = client.get(f"/api/book/{slugA}/calendar.ics?t={tok}")
    check("iCal 订阅(正确 token) -> 200", r.status_code == 200 and "BEGIN:VCALENDAR" in r.text)
    r = client.get(f"/api/book/{slugA}/calendar.ics?t=wrongtoken")
    check("iCal 订阅(错误 token) -> 403", r.status_code == 403)

    # ---------- 13. 顾客 ICS 含姓名/邮箱 ----------
    r = client.get(f"/api/book/{slugA}/booking/{bookingA_id}/ics?token={bookingA_token}")
    check("顾客 ICS 含姓名 Alice", r.status_code == 200 and "Alice" in r.text)

    # ---------- 14. 取消预约实时同步到 iCal 订阅源 ----------
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-17")
    slotX = r.json()[0]["start_local"]
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotX,
              "customer_name": "Dave", "customer_email": "dave@example.com"})
    check("为取消同步测试新建预约 Dave -> 200", r.status_code == 200)
    bidX = r.json()["id"]
    r = client.get(f"/api/book/{slugA}/calendar.ics?t={tok}")
    check("取消前订阅源含 Dave 预约", f"UID:{bidX}@" in r.text)
    r = client.patch(f"/api/admin/bookings/{bidX}",
        headers={"Authorization": f"Bearer {tokenA}"}, json={"status": "cancelled"})
    check("取消 Dave 预约 -> 200", r.status_code == 200)
    r = client.get(f"/api/book/{slugA}/calendar.ics?t={tok}")
    check("取消后订阅源不再含该预约(实时同步)", r.status_code == 200 and f"UID:{bidX}@" not in r.text)

    # ---------- 15. 顾客自助管理链接 (manage/{token}) ----------
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-18")
    slotS = r.json()[0]["start_local"]
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotS,
              "customer_name": "Eve", "customer_email": "eve@example.com"})
    check("自助：顾客下单成功且返回 manage_url", r.status_code == 200 and "/manage/" in r.json().get("manage_url", ""))
    mtoken = r.json()["manage_url"].split("/")[-1]
    r = client.get(f"/api/book/{slugA}/manage/{mtoken}")
    check("自助：凭 token 查看预约详情 -> 200", r.status_code == 200 and r.json().get("customer_name") == "Eve")
    r = client.post(f"/api/book/{slugA}/manage/{mtoken}/cancel")
    check("自助：顾客取消预约 -> 200", r.status_code == 200)
    r = client.get(f"/api/book/{slugA}/manage/{mtoken}")
    check("自助：取消后状态为 cancelled", r.json().get("status") == "cancelled")

    # 自助改期：先建一单，再改到另一天
    # 复用 2026-09-18：Eve 已取消，该时段应被释放、可被 Fiona 重新预约
    # （验证「部分唯一索引」只约束有效预约，取消后时段可回收）
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-18")
    fiona_slot = r.json()[0]["start_local"]
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": fiona_slot,
              "customer_name": "Fiona", "customer_email": "fiona@example.com"})
    ftoken = r.json()["manage_url"].split("/")[-1]
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-20")
    newslot = r.json()[0]["start_local"]
    r = client.post(f"/api/book/{slugA}/manage/{ftoken}/reschedule",
        json={"service_id": svcA_id, "start_local": newslot,
              "customer_name": "Fiona", "customer_email": "fiona@example.com"})
    check("自助：顾客改期 -> 200", r.status_code == 200)
    r = client.get(f"/api/book/{slugA}/manage/{ftoken}")
    check("自助：改期后时间已更新", r.json().get("start_local") == newslot)

    # ---------- 16. 循环预约 (recurring) ----------
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-21")
    slotR = r.json()[0]["start_local"]
    r = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotR,
              "customer_name": "Greg", "customer_email": "greg@example.com", "repeat_weeks": 4})
    check("循环：repeat_weeks=4 创建 4 单", r.status_code == 200 and len(r.json().get("series", [])) == 4)

    # 16b. 循环预约跨夏令时墙钟时间不变（2026-09-27 NZ 由 +12 进入 +13）
    # 若用 (aware 时间 + timedelta(weeks))，跨 DST 后墙钟会整体漂移 1 小时。
    _series = r.json().get("series", [])
    _times = {s["start_local"].split("T")[1][:5] for s in _series}
    check("循环：跨 DST 墙钟时间保持一致(无 1 小时漂移)",
          len(_series) == 4 and len(_times) == 1)

    # ---------- 17. 每日容量 (daily capacity) ----------
    client.patch("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA}"},
                 json={"daily_capacity": 1})
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-22")
    slotC1 = r.json()[0]["start_local"]
    r1 = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotC1,
              "customer_name": "Hana", "customer_email": "hana@example.com"})
    check("容量：当日首单成功", r1.status_code == 200)
    r2 = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotC1,
              "customer_name": "Ivan", "customer_email": "ivan@example.com"})
    check("容量：达上限后第二单被拒(409)", r2.status_code == 409)
    client.patch("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA}"},
                 json={"daily_capacity": 0})  # 还原不限

    # 17b. 每日容量：已完成的预约(done)不应再占用当天未来容量
    client.patch("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA}"},
                 json={"daily_capacity": 1})
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-25")
    slotCap = r.json()[0]["start_local"]
    rK = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotCap,
              "customer_name": "Ken", "customer_email": "ken@example.com"})
    check("容量(done): 首单成功", rK.status_code == 200)
    bidK = rK.json()["id"]
    client.patch(f"/api/admin/bookings/{bidK}",
        headers={"Authorization": f"Bearer {tokenA}"}, json={"status": "done"})
    rL = client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": slotCap,
              "customer_name": "Lily", "customer_email": "lily@example.com"})
    check("容量(done): 已完成单不占容量 -> 第二单成功(200)",
          rL.status_code == 200)
    client.patch("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA}"},
                 json={"daily_capacity": 0})  # 还原不限

    # ---------- 18. CSV 导出 + 简易报表 ----------
    r = client.get("/api/admin/export-csv", headers={"Authorization": f"Bearer {tokenA}"})
    check("CSV 导出 200 且为 text/csv", r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""))
    check("CSV 含表头与顾客行", "customer_name" in r.text and "eve@example.com" in r.text)
    r = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {tokenA}"})
    st = r.json()
    check("报表：返回 total / week_bookings / no_show_rate",
          r.status_code == 200 and "total" in st and "week_bookings" in st and "no_show_rate" in st)
    check("报表：total 反映已有预约(>=10)", st.get("total", 0) >= 10)

    # ---------- 19. 老顾客识别 (returning customer) ----------
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-23")
    sH1 = r.json()[0]["start_local"]
    client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": sH1,
              "customer_name": "Henry", "customer_email": "henry@example.com"})
    r = client.get(f"/api/book/{slugA}/availability?service_id={svcA_id}&date=2026-09-24")
    sH2 = r.json()[0]["start_local"]
    client.post(f"/api/book/{slugA}/bookings",
        json={"service_id": svcA_id, "start_local": sH2,
              "customer_name": "Henry", "customer_email": "henry@example.com"})
    r = client.get("/api/admin/bookings", headers={"Authorization": f"Bearer {tokenA}"},
                   params={"q": "henry"})
    henry = [b for b in r.json() if b.get("email") == "henry@example.com"]
    check("老顾客：Henry 有 2 次到店记录", len(henry) == 2)
    check("老顾客：visits>=2 且 returning=true",
          all(b.get("visits", 0) >= 2 and b.get("returning") is True for b in henry))

    # ---------- 20. JWT Token 强制失效（token_version 机制） ----------
    from auth_utils import create_token, decode_token

    # 20a. 登录 token 携带 token_version
    r = client.post("/api/admin/login", json={"username": "bossA", "password": "pwA123"})
    tokenA_tv = r.json()["token"]
    payload = decode_token(tokenA_tv)
    check("20a Token payload 含 token_version", payload is not None and "token_version" in payload)

    # 20b. 平滑迁移：旧格式 Token（无 token_version）→ 401
    legacy = create_token({"sub": 2, "shop_id": 2, "role": "shop_owner"})
    r = client.get("/api/admin/shop", headers={"Authorization": f"Bearer {legacy}"})
    check("20b 旧格式 Token(无 token_version) -> 401", r.status_code == 401)

    # 20c. 修改密码 → 旧 Token 立即失效；新密码可重新登录
    r = client.post("/api/admin/change-password",
        headers={"Authorization": f"Bearer {tokenA_tv}"},
        json={"old_password": "pwA123", "new_password": "pwANew789"})
    check("20c 修改密码成功 -> 200", r.status_code == 200)
    r = client.get("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA_tv}"})
    check("20c 改密后旧 Token -> 401", r.status_code == 401)
    r = client.post("/api/admin/login", json={"username": "bossA", "password": "pwANew789"})
    check("20c 新密码登录成功", r.status_code == 200)
    tokenA_new = r.json()["token"]
    r = client.get("/api/admin/shop", headers={"Authorization": f"Bearer {tokenA_new}"})
    check("20c 新 Token 可用 -> 200", r.status_code == 200)
    # 还原密码，避免影响后续测试
    client.post("/api/admin/change-password",
        headers={"Authorization": f"Bearer {tokenA_new}"},
        json={"old_password": "pwANew789", "new_password": "pwA123"})

    # 20d. 主动登出 → 当前 Token 失效
    r = client.post("/api/admin/login", json={"username": "bossB", "password": "pwB123"})
    tokenB2 = r.json()["token"]
    r = client.post("/api/admin/logout", headers={"Authorization": f"Bearer {tokenB2}"})
    check("20d 主动登出 -> 200", r.status_code == 200)
    r = client.get("/api/admin/shop", headers={"Authorization": f"Bearer {tokenB2}"})
    check("20d 登出后旧 Token -> 401", r.status_code == 401)
    r = client.post("/api/admin/login", json={"username": "bossB", "password": "pwB123"})
    tokenB = r.json()["token"]  # 更新 tokenB 供后续使用

    # 20e. 超管重置商家密码 → 该商家旧 Token 失效
    r = client.post(f"/api/super-admin/shops/{rB.json()['shop_id']}/reset-password",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"new_password": "pwBReset9"})
    check("20e 超管重置密码 -> 200", r.status_code == 200)
    r = client.get("/api/admin/shop", headers={"Authorization": f"Bearer {tokenB}"})
    check("20e 重置后商家旧 Token -> 401", r.status_code == 401)
    r = client.post("/api/admin/login", json={"username": "bossB", "password": "pwBReset9"})
    check("20e 重置后新密码登录成功", r.status_code == 200)
    tokenB = r.json()["token"]
    # 还原密码
    client.post("/api/admin/change-password",
        headers={"Authorization": f"Bearer {tokenB}"},
        json={"old_password": "pwBReset9", "new_password": "pwB123"})

    # 20f. 超管 Token 不受版本机制影响（无 users 行）
    r = client.get("/api/super-admin/shops", headers={"Authorization": f"Bearer {super_token}"})
    check("20f 超管 Token 始终有效 -> 200", r.status_code == 200)

print(f"\n==== {passed} checks passed, {failed} failed ====")
if failed:
    raise SystemExit(1)
