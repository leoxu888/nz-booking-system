"""免费冒烟测试：不联网、不花钱，验证核心接口能跑通。
覆盖当前的多租户路由（按 shop slug 隔离）。"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# 必须在 import main 之前设置 DB_PATH，否则 database 模块在导入时已定格默认库路径，
# 导致测试偷偷写进真实的 booking.db。
os.environ["DB_PATH"] = "/tmp/smoke_test.db"
for f in ("/tmp/smoke_test.db", "/tmp/smoke_test.db-wal", "/tmp/smoke_test.db-shm"):
    try: os.remove(f)
    except OSError: pass

from datetime import date, timedelta
from fastapi.testclient import TestClient
import main

passed = []
failed = []

def check(name, cond):
    (passed if cond else failed).append(name)
    print(("OK  " if cond else "FAIL") + " - " + name)

with TestClient(main.app) as c:
    # 1. 演示店铺可访问
    shop = c.get("/api/book/demo/shop").json()
    check("GET /api/book/demo/shop 返回演示店铺", shop.get("slug") == "demo")

    sid = shop["id"] if "id" in shop else None
    d = (date.today() + timedelta(days=7)).isoformat()

    # 2. 服务列表
    svcs = c.get("/api/book/demo/services").json()
    check("GET /api/book/demo/services 返回服务", isinstance(svcs, list) and len(svcs) >= 1)
    svc_id = svcs[0]["id"]

    # 3. 可约时段（未来日期应有空档）
    slots = c.get(f"/api/book/demo/availability?service_id={svc_id}&date={d}").json()
    check("GET /api/book/demo/availability 返回空档", isinstance(slots, list) and len(slots) >= 1)

    # 4. 顾客下单（姓名 + 邮箱）
    bk = c.post(f"/api/book/demo/bookings", json={
        "service_id": svc_id,
        "start_local": slots[0]["start_local"],
        "customer_name": "Smoke Test",
        "customer_email": "smoke@example.com",
    }).json()
    check("POST /api/book/demo/bookings 创建成功", "id" in bk)

    # 5. 同一时段重复下单应被拒（409）—— 验证唯一时段约束生效
    bk2 = c.post(f"/api/book/demo/bookings", json={
        "service_id": svc_id,
        "start_local": slots[0]["start_local"],
        "customer_name": "Smoke Test 2",
        "customer_email": "smoke2@example.com",
    })
    check("重复时段下单被拒(409)", bk2.status_code == 409)

    # 6. 顾客 ICS 需要 token（不再可被顺序枚举）
    ics_no = c.get(f"/api/book/demo/booking/{bk['id']}/ics")
    check("无 token 访问 ICS 被拒(404)", ics_no.status_code == 404)
    tok = bk.get("manage_url", "").split("/")[-1]
    ics_yes = c.get(f"/api/book/demo/booking/{bk['id']}/ics?token={tok}")
    check("带 token 访问 ICS 成功", ics_yes.status_code == 200 and "BEGIN:VCALENDAR" in ics_yes.text)

    # 7. 二维码
    qr = c.get("/api/book/demo/qr")
    check("GET /api/book/demo/qr 返回图片", qr.status_code == 200 and qr.headers["content-type"] == "image/png")

    # 8. 老板登录
    tok_admin = c.post("/api/admin/login", json={"username": "admin", "password": "admin123"}).json().get("token")
    check("POST /api/admin/login 拿到令牌", bool(tok_admin))
    h = {"Authorization": f"Bearer {tok_admin}"}

    # 9. 未授权访问后台被拦
    noauth = c.get("/api/admin/shop")
    check("未带令牌访问后台被拒(401)", noauth.status_code == 401)

    # 10. 后台查看本店排班（含刚建的预约）
    future = c.get(f"/api/admin/bookings?date={d}", headers=h).json()
    check("GET /api/admin/bookings?date= 含新预约", any(b["id"] == bk["id"] for b in future))

    # 11. 新增服务
    ns = c.post("/api/admin/services", headers=h,
                json={"name": "Piano", "duration_min": 45, "price": 40}).json()
    check("POST /api/admin/services 新增服务", "id" in ns)

    # 12. 改状态为完成
    st = c.patch(f"/api/admin/bookings/{bk['id']}", headers=h, json={"status": "done"}).json()
    check("PATCH 状态更新成功", st.get("ok") is True)

print(f"\n结果：通过 {len(passed)} / 失败 {len(failed)}")
if failed:
    print("失败项：", failed)
    sys.exit(1)
print("🎉 全部通过，免费 MVP 可运行。")
