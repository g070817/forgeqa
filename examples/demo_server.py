#!/usr/bin/env python3
"""examples/demo_server.py — 内置演示站点。

作用：让 ForgeQA 开箱即跑。它模拟一个**有真实校验逻辑**的最小业务系统，
所以用例跑出来是有意义的，不是对着假接口自娱自乐：

- 账号密码登录 → 发 token，接口需要 Bearer 鉴权
- 用户 CRUD：字段校验（必填/长度/枚举/区间）、唯一约束（username/email 冲突返回 409）
- 订单接口：带业务不变量（下单后用户订单数 +1）
- ``/ui/form``：一个真实表单页面，供 Playwright 做 UI 回归

关键：**所有异常输入都必须返回 4xx + 明确业务码，不得出现 5xx。**
这条不变量本身就是最好的回归断言。

启动::

    python examples/demo_server.py --port 8000
    # 或
    forgeqa demo --port 8000
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g, jsonify, request, Response

DEFAULT_DB = Path(__file__).resolve().parent.parent / "out" / "forgeqa.db"
ROLES = ("user", "admin")
DEPTS = ("研发部", "测试部", "产品部")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PAGE_SIZE = 10

DDL = """
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  username   TEXT NOT NULL UNIQUE,
  email      TEXT NOT NULL UNIQUE,
  phone      TEXT,
  age        INTEGER,
  role       TEXT NOT NULL DEFAULT 'user',
  dept       TEXT,
  vip        INTEGER NOT NULL DEFAULT 0,
  status     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no   TEXT NOT NULL UNIQUE,
  user_id    INTEGER NOT NULL,
  amount     REAL NOT NULL,
  currency   TEXT NOT NULL DEFAULT 'CNY',
  status     TEXT NOT NULL DEFAULT 'created',
  created_at TEXT
);
"""


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB"] = str(db_path or DEFAULT_DB)

    # ---------------- 基础设施 ----------------
    def conn() -> sqlite3.Connection:
        if "db" not in g:
            c = sqlite3.connect(app.config["DB"], timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            g.db = c
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db() -> None:
        Path(app.config["DB"]).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(app.config["DB"])
        c.executescript(DDL)
        c.commit()
        c.close()

    app.config["INIT_DB"] = init_db
    init_db()

    def fail(code: int, message: str, http: int = 400, detail=None) -> Response:
        payload = {"code": code, "message": message}
        if detail:
            payload["detail"] = detail
        resp = jsonify(payload)
        resp.status_code = http
        return resp

    def ok(data=None, http: int = 200, **extra) -> Response:
        payload = {"code": 0, "message": "ok", "data": data}
        payload.update(extra)
        resp = jsonify(payload)
        resp.status_code = http
        return resp

    def current_user():
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        return token.replace("demo-token-", "") if token.startswith("demo-token-") else None

    def require_auth():
        user = current_user()
        if user is None:
            return fail(4010, "未登录或 token 无效", http=401)
        return None

    def now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------------- 基础接口 ----------------
    @app.get("/health")
    def health():
        return jsonify({"status": "up", "time": now(), "db": Path(app.config["DB"]).name})

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        accounts = {"admin": "admin123", "qa": "qa123456"}
        guard = None if accounts.get(username) == password else fail(1002, "账号或密码错误", http=401)
        if guard:
            return guard
        return ok({
            "token": f"demo-token-{username}",
            "username": username,
            "expires_in": 7200,
            "roles": ["admin"] if username == "admin" else ["user"],
        })

    # ---------------- 用户 ----------------
    def validate_user(body: dict, *, partial: bool = False) -> tuple[dict, Response | None]:
        errors: list[dict[str, str]] = []
        clean: dict = {}

        def need(key: str, required: bool):
            if key in body:
                return True, body[key]
            if required and not partial:
                errors.append({"field": key, "reason": "必填"})
            return False, None

        present, name = need("name", True)
        if present:
            if not isinstance(name, str):
                errors.append({"field": "name", "reason": f"必须是字符串，收到 {type(name).__name__}"})
            elif not (2 <= len(name.strip()) <= 20):
                errors.append({"field": "name", "reason": f"长度需在 2-20，实际 {len(name)}"})
            else:
                clean["name"] = name.strip()

        present, username = need("username", True)
        if present:
            if not isinstance(username, str):
                errors.append({"field": "username", "reason": "必须是字符串"})
            elif not USERNAME_RE.match(username):
                errors.append({"field": "username", "reason": "需为 3-20 位字母/数字/下划线"})
            else:
                clean["username"] = username

        present, email = need("email", True)
        if present:
            if not isinstance(email, str):
                errors.append({"field": "email", "reason": "必须是字符串"})
            elif not EMAIL_RE.match(email) or len(email) > 64:
                errors.append({"field": "email", "reason": "邮箱格式不合法"})
            else:
                clean["email"] = email.lower()

        if "age" in body:
            age = body["age"]
            if isinstance(age, bool) or not isinstance(age, (int, float)) or int(age) != age:
                errors.append({"field": "age", "reason": "必须是整数"})
            elif not (18 <= int(age) <= 65):
                errors.append({"field": "age", "reason": f"需在 18-65，实际 {int(age)}"})
            else:
                clean["age"] = int(age)

        if "role" in body:
            role = body["role"]
            if role not in ROLES:
                errors.append({"field": "role", "reason": f"必须是 {list(ROLES)} 之一"})
            else:
                clean["role"] = role

        if "dept" in body and body["dept"] is not None:
            if body["dept"] not in DEPTS:
                errors.append({"field": "dept", "reason": f"必须是 {list(DEPTS)} 之一"})
            else:
                clean["dept"] = body["dept"]

        if "phone" in body and body["phone"] is not None:
            phone = str(body["phone"])
            if len(phone) > 20 or not re.match(r"^[0-9+\-\s]{5,20}$", phone):
                errors.append({"field": "phone", "reason": "手机号格式不合法"})
            else:
                clean["phone"] = phone

        if "vip" in body:
            clean["vip"] = 1 if body["vip"] in (True, 1, "true", "1") else 0

        if errors:
            return {}, fail(1400, "参数校验失败", http=422, detail={"errors": errors})
        return clean, None

    @app.post("/api/users")
    def create_user():
        guard = require_auth()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if body is None or not isinstance(body, dict):
            return fail(1400, "请求体必须是 JSON 对象", http=422)
        if len(request.get_data(as_text=True)) > 100_000:
            return fail(1413, "请求体过大", http=413)

        clean, err = validate_user(body)
        if err:
            return err
        clean.setdefault("phone", None)
        clean.setdefault("age", None)
        clean.setdefault("role", "user")
        clean.setdefault("dept", None)
        clean.setdefault("vip", 0)

        db = conn()
        dup = db.execute("SELECT id, username, email FROM users WHERE username = ? OR email = ?",
                         (clean["username"], clean["email"])).fetchone()
        if dup:
            same_email = dup["email"] == clean["email"]
            return fail(1409,
                        "邮箱已被占用" if same_email else "用户名已存在",
                        http=409,
                        detail={"conflict": "email" if same_email else "username", "id": dup["id"]})
        try:
            cur = db.execute(
                """INSERT INTO users (name, username, email, phone, age, role, dept, vip, status, created_at)
                   VALUES (:name, :username, :email, :phone, :age, :role, :dept, :vip, 1, :created_at)""",
                {**clean, "created_at": now()},
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            return fail(1409, f"数据冲突: {exc}", http=409)
        return ok({**clean, "id": cur.lastrowid, "status": 1,
                   "created_at": now(), "operator": current_user()}, http=201)

    @app.get("/api/users")
    def list_users():
        guard = require_auth()
        if guard:
            return guard
        page = max(int(request.args.get("page", 1)), 1)
        size = min(int(request.args.get("size", PAGE_SIZE)), 100)
        keyword = (request.args.get("keyword") or "").strip()
        where, params = "WHERE 1=1", []
        if keyword:
            where += " AND (name LIKE ? OR email LIKE ?)"
            params += [f"%{keyword}%"] * 2
        db = conn()
        total = db.execute(f"SELECT COUNT(*) FROM users {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM users {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall()
        return ok({"list": [dict(r) for r in rows], "total": total, "page": page, "size": size},
                  http=200)

    @app.get("/api/users/<int:uid>")
    def get_user(uid: int):
        guard = require_auth()
        if guard:
            return guard
        row = conn().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return fail(1404, f"用户 {uid} 不存在", http=404)
        orders = conn().execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (uid,)).fetchone()[0]
        return ok({**dict(row), "order_count": orders})

    @app.delete("/api/users/<int:uid>")
    def delete_user(uid: int):
        guard = require_auth()
        if guard:
            return guard
        db = conn()
        row = db.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return fail(1404, f"用户 {uid} 不存在", http=404)
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        db.commit()
        return ok({"id": uid, "deleted": 1})

    # ---------------- 订单（带业务不变量） ----------------
    @app.post("/api/orders")
    def create_order():
        guard = require_auth()
        if guard:
            return guard
        body = request.get_json(silent=True) or {}
        user_id = body.get("user_id")
        amount = body.get("amount")
        if not isinstance(user_id, int):
            return fail(1400, "user_id 必须是整数", http=422)
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return fail(1400, "amount 必须是数字", http=422)
        if amount <= 0:
            return fail(1400, "amount 必须大于 0", http=422)

        db = conn()
        if not db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
            return fail(1404, f"用户 {user_id} 不存在", http=404)
        before = db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)).fetchone()[0]
        order_no = body.get("order_no") or f"QA{int(time.time() * 1000) % 10 ** 10:010d}"
        try:
            cur = db.execute(
                """INSERT INTO orders (order_no, user_id, amount, currency, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_no, user_id, amount, body.get("currency", "CNY"),
                 body.get("status", "created"), now()),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            return fail(1409, f"订单号冲突: {exc}", http=409)
        after = db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)).fetchone()[0]
        return ok({"id": cur.lastrowid, "order_no": order_no, "user_id": user_id,
                   "amount": amount, "orders_before": before, "orders_after": after}, http=201)

    @app.get("/api/orders")
    def list_orders():
        guard = require_auth()
        if guard:
            return guard
        rows = conn().execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()
        return ok({"list": [dict(r) for r in rows],
                   "total": conn().execute("SELECT COUNT(*) FROM orders").fetchone()[0]})

    # ---------------- UI 页面 ----------------
    @app.get("/ui/form")
    def ui_form():
        return Response(UI_PAGE, mimetype="text/html; charset=utf-8")

    @app.get("/")
    def index():
        return Response(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>ForgeQA 演示站点</title></head><body style="font-family:system-ui;max-width:640px;margin:60px auto">
<h2>ForgeQA 演示站点</h2>
<p>这是一个可被造数与自动回归的靶场：接口有真实校验逻辑，UI 页面有真实交互。</p>
<ul>
  <li><a href="/health">/health</a> 健康检查</li>
  <li><a href="/api/users">GET /api/users</a> 用户列表（需 Bearer token）</li>
  <li><a href="/ui/form">/ui/form</a> UI 回归用表单页</li>
</ul>
<pre>POST /api/login  {{"username":"admin","password":"admin123"}}</pre>
<p>数据库: {Path(app.config['DB'])}</p>
</body></html>""", mimetype="text/html; charset=utf-8")

    @app.errorhandler(404)
    def _404(_e):
        return fail(1404, "接口不存在", http=404)

    @app.errorhandler(405)
    def _405(_e):
        return fail(1405, "方法不允许", http=405)

    @app.errorhandler(Exception)
    def _500(exc):  # 兜底：不允许裸 5xx，统一转成结构化错误
        app.logger.exception("未捕获异常")
        return fail(1500, f"服务内部错误: {type(exc).__name__}", http=500)

    return app


# --------------------------------------------------------------------------- #
# UI 页面：表单提交 + 校验提示 + 成功文案，供 Playwright 断言
# --------------------------------------------------------------------------- #
UI_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>用户注册 · ForgeQA Demo</title>
<style>
 body{font-family:-apple-system,"PingFang SC",system-ui;background:#f5f6f8;margin:0;padding:40px 16px;color:#1f2328}
 .card{max-width:520px;margin:0 auto;background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:24px 26px}
 h1{font-size:19px;margin:0 0 4px}.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
 label{display:block;font-size:13px;margin:12px 0 5px;font-weight:600}
 input,select{width:100%;padding:9px 11px;border:1px solid #d6dae0;border-radius:7px;font-size:14px;background:#fff;color:#1f2328}
 .row{display:flex;gap:12px}.row>div{flex:1}
 button{margin-top:18px;width:100%;padding:10px;border:0;border-radius:8px;background:#1d4ed8;color:#fff;
   font-size:15px;font-weight:600;cursor:pointer}
 button:disabled{background:#9aa4b2;cursor:not-allowed}
 #msg{margin-top:14px;font-size:13px;display:none;padding:10px 12px;border-radius:8px}
 #msg.ok{display:block;background:#e7f6ec;color:#15803d;border:1px solid #bfe3cb}
 #msg.err{display:block;background:#fdeaea;color:#dc2626;border:1px solid #f3c8c8}
</style></head><body>
<div class="card">
  <h1>创建用户</h1>
  <div class="sub">演示站点 · 提交后会真实写入数据库并返回业务码</div>
  <form id="userForm">
    <div class="row">
      <div><label for="name">姓名 *</label><input id="name" name="name" placeholder="请输入姓名" autocomplete="off"></div>
      <div><label for="age">年龄</label><input id="age" name="age" type="number" placeholder="18-65"></div>
    </div>
    <label for="username">用户名 *</label><input id="username" name="username" placeholder="3-20 位字母数字下划线">
    <label for="email">邮箱 *</label><input id="email" name="email" type="text" placeholder="name@example.com">
    <label for="dept">部门</label>
    <select id="dept" name="dept">
      <option value="">请选择</option>
      <option value="研发部">研发部</option>
      <option value="测试部">测试部</option>
      <option value="产品部">产品部</option>
    </select>
    <button type="submit" id="submitBtn">提交</button>
  </form>
  <div id="msg"></div>
</div>
<script>
const TOKEN = 'demo-token-admin';
document.getElementById('userForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = document.getElementById('msg');
  const btn = document.getElementById('submitBtn');
  msg.className = ''; msg.style.display = 'none';
  btn.disabled = true; btn.textContent = '提交中…';
  const body = {};
  for (const el of new FormData(e.target).entries()) if (el[1] !== '') body[el[0]] = el[1];
  if (body.age) body.age = parseInt(body.age, 10);
  try {
    const r = await fetch('/api/users', {method:'POST', headers:{
      'Content-Type':'application/json', 'Authorization':'Bearer '+TOKEN}, body:JSON.stringify(body)});
    const j = await r.json();
    if (r.status === 201) {
      msg.className = 'ok';
      msg.textContent = '创建成功，用户 ID：' + j.data.id;
      document.getElementById('createdId').textContent = j.data.id;
      document.getElementById('resultBox').style.display = 'block';
      e.target.reset();
    } else {
      msg.className = 'err';
      const detail = j.detail && j.detail.errors ? j.detail.errors.map(x=>x.field+': '+x.reason).join('; ') : '';
      msg.textContent = '创建失败（' + r.status + '）：' + (j.message || '') + (detail ? ' — ' + detail : '');
    }
  } catch (err) {
    msg.className = 'err'; msg.textContent = '请求异常：' + err.message;
  } finally {
    btn.disabled = false; btn.textContent = '提交';
  }
});
</script>
<div id="resultBox" style="display:none;margin-top:12px;font-size:13px;color:#6b7280">
  最近创建的用户 ID：<b id="createdId">-</b>
</div>
</body></html>
"""


def main(host: str = "127.0.0.1", port: int = 8000, db: str | None = None) -> None:
    app = create_app(db)
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ForgeQA 演示站点")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None)
    ns = ap.parse_args()
    print(f"ForgeQA 演示站点 → http://{ns.host}:{ns.port}   数据库: {ns.db or DEFAULT_DB}")
    main(ns.host, ns.port, ns.db)
