#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import gzip
import http.client
import http.server
import json
import mimetypes
import os
import re
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse
import urllib.request

CLIENT_ROOT = os.environ.get("PKM_CLIENT_ROOT", "/app/pgame/client/pkm_client")
GM_ROOT = os.environ.get("PKM_GM_ROOT", "/app/pgame/Server/ManagementService/web")
DATA_DIR = os.environ.get("PKM_DATA_DIR", "/home/user/data")
MYSQL_SOCK = os.environ.get("MYSQL_SOCK", "/tmp/mysqld.sock")
LISTEN_PORT = int(os.environ.get("PORT", "7860"))

os.makedirs(DATA_DIR, exist_ok=True)
ORDERS_FILE = os.path.join(DATA_DIR, "pay_orders.json")
CONFIG_FILE = os.path.join(DATA_DIR, "sepay_config.json")
PROCESSED_FILE = os.path.join(DATA_DIR, "sepay_processed.json")
GAS_CODE_FILE = "/app/pgame/SePay_Code.gs"

ORDERS_LOCK = threading.Lock()
GZIP_CACHE_LOCK = threading.Lock()
GZIP_CACHE = {}

VND_PER_YUAN = 1000
SEPAY_SECRET = "PKM_SEPAY_2026"

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".fnt": "text/plain; charset=utf-8",
    ".exml": "application/xml; charset=utf-8",
}

COMPRESSIBLE_EXTS = {".html", ".htm", ".js", ".json", ".css", ".xml", ".csv", ".txt", ".svg", ".fnt", ".exml"}

VND_BY_ID = {
    1: 20000, 2: 60000, 3: 100000, 4: 150000,
    5: 200000, 6: 500000, 7: 1000000, 8: 2000000,
    9: 50000, 10: 100000,
}


def get_expected_vnd(goodsid, rmb):
    return VND_BY_ID.get(int(goodsid), max(20000, int(rmb * VND_PER_YUAN)))


PACK_TABLE = {
    1: (6, 66), 2: (30, 330), 3: (50, 550), 4: (100, 1100),
    5: (200, 2200), 6: (500, 6000), 7: (1000, 13000), 8: (2000, 27000),
    9: (30, 0), 10: (98, 0), 11: (1, 0), 12: (998, 0), 13: (49, 0),
    14: (688, 0), 15: (28, 0),
    20: (6, 0), 21: (30, 0), 22: (6, 0), 23: (30, 0), 24: (6, 0), 25: (30, 0),
    26: (6, 0), 27: (6, 0), 28: (30, 0), 29: (30, 0), 30: (6, 0), 31: (98, 0),
    32: (6, 0), 33: (30, 0), 34: (6, 0), 35: (30, 0), 36: (6, 0), 37: (30, 0),
    38: (6, 0), 39: (6, 0), 40: (60, 0), 41: (60, 0), 42: (6, 0), 43: (98, 0),
    56: (6, 0), 57: (30, 0), 58: (98, 0), 59: (168, 0), 60: (328, 0), 61: (648, 0),
}

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

GM_API_PREFIXES = (
    "/account/", "/server/", "/player/", "/mail/", "/activity/", "/announce/",
    "/fake_recharge/", "/recharge/", "/statistics/", "/platform/", "/cdkey/",
    "/operation/", "/system/", "/ban/", "/gift/", "/guild/", "/rank/",
    "/white/", "/log/", "/notice/", "/mute/", "/item/", "/elf/", "/order/",
)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_orders():
    with ORDERS_LOCK:
        return load_json(ORDERS_FILE, [])


def save_orders(orders):
    with ORDERS_LOCK:
        save_json(ORDERS_FILE, orders)


def ensure_gm_session():
    sess = "sepay-auto-session-pkm-2026"
    sql = f"REPLACE INTO gm_session(session, account, lastActive) VALUES ('{sess}', '001', NOW());"
    subprocess.run(
        ["mysql", f"--socket={MYSQL_SOCK}", "-u", "root", "-pqweqwe", "-D", "pokemon", "-e", sql],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return sess


def gm_login_and_recharge(sid, pid, goodsid, money, diamond):
    session = "sepay-auto-session-pkm-2026"
    res = {}
    for attempt in range(2):
        payload = urllib.parse.urlencode({
            "sid": sid,
            "pid": pid,
            "goodsid": goodsid,
            "money": money,
            "diamond": diamond,
            "session": session,
        }).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:18002/fake_recharge/create", data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode("utf-8"))
        if res.get("status") == 0 and res.get("result"):
            return res
        if attempt == 0:
            ensure_gm_session()
    return res


def gm_send_mail(sid, pid, title, detail, items_str):
    session = "sepay-auto-session-pkm-2026"
    res = {}
    for attempt in range(2):
        payload = urllib.parse.urlencode({
            "sid": sid,
            "pid": pid,
            "title": title,
            "detail": detail,
            "items": items_str,
            "session": session,
        }).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:18002/mail/add", data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode("utf-8"))
        if res.get("status") == 0 and res.get("result"):
            return res
        if attempt == 0:
            ensure_gm_session()
    return res


FC7_LOCK = threading.Lock()
FC7_TABLE_READY = False
FC7_CHOICES = {1243: "Raikou", 1244: "Entei", 1245: "Suicune"}
FC7_REWARDS = {
    1: ("Quà Nạp Đầu - Ngày 1", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 1!", "10507;1|2;10000|1;500000"),
    2: ("Quà Nạp Đầu - Ngày 2", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 2!", "10131;3|13002;10|2151;10"),
    3: ("Quà Nạp Đầu - Ngày 3", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 3!", "10013;20|12003;50|10309;3"),
    4: ("Quà Nạp Đầu - Ngày 4", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 4!", "12151;40|12152;20|10166;1"),
    5: ("Quà Nạp Đầu - Ngày 5", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 5!", "12111;50|12201;20|10169;1"),
    6: ("Quà Nạp Đầu - Ngày 6", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 6!", "10089;30|10090;50|2150;10"),
    7: ("Quà Nạp Đầu - Ngày 7", "Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 7 + Thần Thú tự chọn!", "13003;3|12111;100|13013;2"),
}


def _mysql_query(sql):
    p = subprocess.run(
        ["mysql", f"--socket={MYSQL_SOCK}", "-u", "root", "-pqweqwe", "-D", "pokemon", "-N", "-B", "-e", sql],
        capture_output=True, text=True, timeout=8
    )
    return p.returncode, p.stdout.strip()


def fc7_ensure_table():
    global FC7_TABLE_READY
    if FC7_TABLE_READY:
        return
    sql = (
        "CREATE TABLE IF NOT EXISTS t_first_charge_7d ("
        "pid INT PRIMARY KEY,"
        "first_charge_ts BIGINT NOT NULL DEFAULT 0,"
        "claimed_days VARCHAR(64) NOT NULL DEFAULT '',"
        "day7_choice INT NOT NULL DEFAULT 0,"
        "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
        ");"
    )
    rc, _ = _mysql_query(sql)
    if rc == 0:
        FC7_TABLE_READY = True


def fc7_record_first_charge(pid, ts=None):
    pid = int(pid)
    if pid <= 0:
        return
    now_ts = int(ts or time.time())
    with FC7_LOCK:
        fc7_ensure_table()
        _mysql_query(
            f"INSERT INTO t_first_charge_7d(pid, first_charge_ts, claimed_days, day7_choice) "
            f"VALUES ({pid}, {now_ts}, '', 0) "
            f"ON DUPLICATE KEY UPDATE first_charge_ts = IF(first_charge_ts=0, {now_ts}, first_charge_ts);"
        )


def _vn_day_num(ts):
    return int((int(ts) + 7 * 3600) // 86400)


def fc7_get_status(pid):
    pid = int(pid)
    if pid <= 0:
        return {"status": 0, "recharged": False, "unlocked_day": 0, "claimed_days": [], "day7_choice": 0}
    with FC7_LOCK:
        fc7_ensure_table()
        rc, out = _mysql_query(f"SELECT first_charge_ts, claimed_days, day7_choice FROM t_first_charge_7d WHERE pid={pid};")
        first_ts = 0
        claimed = []
        choice = 0
        if rc == 0 and out:
            parts = out.splitlines()[0].split("\t")
            first_ts = int(parts[0] or 0)
            claimed = [int(x) for x in (parts[1] if len(parts) > 1 else "").split(",") if x.strip().isdigit()]
            choice = int(parts[2] or 0) if len(parts) > 2 else 0

        if first_ts == 0:
            # Check approved orders in pay_orders.json
            orders = load_orders()
            for o in orders:
                if o.get("status") == "approved" and int(o.get("pid", 0)) == pid:
                    first_ts = int(o.get("ts") or time.time())
                    break
            # Also check if player has real recharge > initial VIP 9 (2688) in DB
            if first_ts == 0:
                rc2, out2 = _mysql_query(f"SELECT COALESCE(MAX(recharge),0) FROM t_player WHERE pid={pid};")
                if rc2 == 0 and out2.strip().isdigit() and int(out2.strip()) > 2688:
                    first_ts = int(time.time())
            if first_ts > 0:
                _mysql_query(
                    f"INSERT INTO t_first_charge_7d(pid, first_charge_ts, claimed_days, day7_choice) "
                    f"VALUES ({pid}, {first_ts}, '', 0) ON DUPLICATE KEY UPDATE first_charge_ts={first_ts};"
                )

    if first_ts <= 0:
        return {"status": 0, "recharged": False, "first_charge_ts": 0, "unlocked_day": 0, "claimed_days": [], "day7_choice": 0}
    unlocked = min(7, max(1, 1 + (_vn_day_num(time.time()) - _vn_day_num(first_ts))))
    return {
        "status": 0,
        "recharged": True,
        "first_charge_ts": first_ts,
        "unlocked_day": unlocked,
        "claimed_days": sorted(set(claimed)),
        "day7_choice": choice,
    }


def fc7_claim_day(pid, day, choice=0):
    pid = int(pid)
    day = int(day)
    choice = int(choice or 0)
    if pid <= 0 or day not in FC7_REWARDS:
        return {"status": -1, "message": "Dữ liệu không hợp lệ"}
    if day == 7 and choice not in FC7_CHOICES:
        return {"status": -1, "message": "Vui lòng chọn 1 Thần Thú (Raikou / Entei / Suicune)!"}

    st = fc7_get_status(pid)
    if not st["recharged"]:
        return {"status": -1, "message": "Bạn cần nạp lần đầu (từ 20.000đ) để mở khóa quà 7 ngày!"}
    if day > st["unlocked_day"]:
        return {"status": -1, "message": f"Quà Ngày {day} chưa mở khóa (hiện tại mới mở đến Ngày {st['unlocked_day']})!"}
    if day in st["claimed_days"]:
        return {"status": -1, "message": f"Bạn đã nhận quà Ngày {day} rồi!"}

    title, detail, items_str = FC7_REWARDS[day]
    if day == 7:
        items_str = f"{items_str}|{choice};1"
        detail = f"Chúc mừng bạn nhận Quà Nạp Lần Đầu Ngày 7 + Thần Thú {FC7_CHOICES[choice]}!"

    res = gm_send_mail(1, pid, title, detail, items_str)
    if not (res.get("status") == 0 and res.get("result")):
        return {"status": -1, "message": f"Lỗi gửi thư quà: {res}"}

    new_claimed = sorted(set(st["claimed_days"] + [day]))
    claimed_str = ",".join(str(x) for x in new_claimed)
    with FC7_LOCK:
        fc7_ensure_table()
        ch_sql = f", day7_choice={choice}" if day == 7 else ""
        _mysql_query(f"UPDATE t_first_charge_7d SET claimed_days='{claimed_str}'{ch_sql} WHERE pid={pid};")

    return {
        "status": 0,
        "claimed_days": new_claimed,
        "day7_choice": choice if day == 7 else st["day7_choice"],
        "message": f"Đã gửi quà Ngày {day} vào Hòm Thư trong game!",
    }


def process_sepay_transaction(tx_id, pid, goodsid, amount_vnd, content, source="SePay"):
    tx_id = str(tx_id)
    with ORDERS_LOCK:
        processed = load_json(PROCESSED_FILE, [])
        if tx_id in processed:
            return {"status": 0, "duplicate": True}

    rmb, diamond = PACK_TABLE.get(int(goodsid), (6, 66))
    expected_vnd = get_expected_vnd(goodsid, rmb)

    if int(amount_vnd) < expected_vnd:
        with ORDERS_LOCK:
            orders = load_json(ORDERS_FILE, [])
            new_id = (orders[-1]["id"] + 1) if orders else 1
            orders.append({
                "id": new_id,
                "ts": int(time.time()),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sid": 1,
                "pid": int(pid),
                "playerName": f"UID {pid} ({source} thiếu tiền)",
                "goodsid": int(goodsid),
                "goodsName": f"Gói #{goodsid} (cần {expected_vnd}đ)",
                "rmb": rmb,
                "diamond": diamond,
                "vnd": int(amount_vnd),
                "note": f"{content} [tx:{tx_id}]",
                "status": "pending",
            })
            save_json(ORDERS_FILE, orders)
            processed.append(tx_id)
            save_json(PROCESSED_FILE, processed[-1000:])
        return {"status": -1, "message": f"Amount {amount_vnd} < {expected_vnd}"}

    res = gm_login_and_recharge(1, int(pid), int(goodsid), rmb, diamond)
    if res.get("status") == 0 and res.get("result"):
        with ORDERS_LOCK:
            processed = load_json(PROCESSED_FILE, [])
            if tx_id not in processed:
                processed.append(tx_id)
                save_json(PROCESSED_FILE, processed[-1000:])
            orders = load_json(ORDERS_FILE, [])
            matched = False
            for o in reversed(orders):
                if o["status"] == "pending" and int(o["pid"]) == int(pid) and int(o["goodsid"]) == int(goodsid):
                    o["status"] = "approved"
                    o["ts"] = int(time.time())
                    o["note"] = f"{content} [TỰ ĐỘNG {source} #{tx_id}]"
                    matched = True
                    break
            if not matched:
                new_id = (orders[-1]["id"] + 1) if orders else 1
                orders.append({
                    "id": new_id,
                    "ts": int(time.time()),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "sid": 1,
                    "pid": int(pid),
                    "playerName": f"UID:{pid} (Auto {source})",
                    "goodsid": int(goodsid),
                    "goodsName": f"Gói #{goodsid} ({rmb} Tệ)",
                    "rmb": rmb,
                    "diamond": diamond,
                    "vnd": int(amount_vnd),
                    "note": f"{content} [#{tx_id}]",
                    "status": "approved",
                })
            save_json(ORDERS_FILE, orders)
        try:
            fc7_record_first_charge(pid)
        except Exception:
            pass
        return {"status": 0, "auto_credited": True}
    return {"status": -1, "message": str(res)}


def poll_apps_script_loop():
    while True:
        try:
            cfg = load_json(CONFIG_FILE, {})
            exec_url = str(cfg.get("exec_url", "")).strip()
            if exec_url.startswith("https://script.google.com/"):
                sep = "&" if "?" in exec_url else "?"
                pull_url = f"{exec_url}{sep}action=pull&key={SEPAY_SECRET}"
                req = urllib.request.Request(pull_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8"))
                items = data.get("items") or []
                acked = []
                for item in items:
                    tx_id = str(item.get("txId", ""))
                    pid = int(item.get("pid", 0))
                    goodsid = int(item.get("goodsid", 1))
                    amount = int(item.get("amount", 0))
                    content = str(item.get("content", ""))
                    if pid > 0 and goodsid > 0:
                        out = process_sepay_transaction(tx_id, pid, goodsid, amount, content, source="AppsScript")
                        if out.get("status") == 0 or out.get("duplicate") or "Amount" in str(out.get("message", "")):
                            acked.append(tx_id)
                if acked:
                    ack_body = json.dumps({"action": "ack", "key": SEPAY_SECRET, "txIds": acked}).encode("utf-8")
                    ack_req = urllib.request.Request(
                        exec_url, data=ack_body, method="POST",
                        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(ack_req, timeout=12):
                        pass
        except Exception:
            pass
        time.sleep(4)


def keep_services_warm_loop():
    time.sleep(5)
    tick = 0
    ext_url = os.environ.get("RENDER_EXTERNAL_URL", "https://pixel-pokemon-cloud.onrender.com").rstrip("/") + "/pay/ping"
    while True:
        urls = [
            "http://127.0.0.1:18000/login/servers",
            f"http://127.0.0.1:{LISTEN_PORT}/pay/ping",
        ]
        if tick % 12 == 11:
            urls.append(ext_url)
        tick += 1
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"Connection": "close", "User-Agent": "PKM-KeepAlive/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    r.read(256)
            except Exception:
                pass
        time.sleep(35)


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quản Trị Nạp Tự Động SePay - Pixel Pokemon Cloud</title>
<style>
  body { font-family: Arial, sans-serif; background:#0f172a; color:#f8fafc; margin:0; padding:16px; }
  h2 { margin:0 0 12px; color:#a78bfa; }
  .box { background:#1e293b; border-radius:12px; padding:14px; margin-bottom:16px; border:1px solid #334155; }
  .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  input, select, button { padding:8px 10px; border-radius:8px; border:1px solid #475569; font-size:14px; }
  input, select { background:#0f172a; color:#fff; }
  button { cursor:pointer; font-weight:700; border:none; }
  .btn-ok { background:#10b981; color:#fff; }
  .btn-no { background:#ef4444; color:#fff; }
  .btn-pri { background:#7c3aed; color:#fff; }
  .btn-sec { background:#0284c7; color:#fff; }
  table { width:100%; border-collapse:collapse; margin-top:8px; font-size:13px; }
  th, td { border-bottom:1px solid #334155; padding:8px 6px; text-align:left; }
  th { color:#94a3b8; }
  .st-pending { color:#fbbf24; font-weight:700; }
  .st-approved { color:#34d399; font-weight:700; }
  .st-rejected { color:#f87171; font-weight:700; }
  code { background:#0f172a; padding:3px 6px; border-radius:6px; color:#38bdf8; }
</style>
</head>
<body>
  <h2>⚡ Hệ Thống Nạp Tự Động SePay - Cloud 24/7</h2>
  <div class="box">
    <div style="font-weight:700;margin-bottom:6px;color:#34d399;">1. Kết nối SePay Tự Động Cộng Tiền 24/7:</div>
    <div style="font-size:13px;color:#cbd5e1;line-height:1.6;margin-bottom:8px;">
      • <b>Webhook trực tiếp cố định:</b> <code id="wh_url"></code><br>
      • <b>Hoặc qua Google Apps Script (/exec):</b> <button class="btn-sec" style="padding:4px 8px;font-size:12px;" onclick="copyGas()">📋 Copy Code.gs</button>
    </div>
    <div class="row">
      <input id="exec_url" type="text" placeholder="Dán link https://script.google.com/macros/s/.../exec (nếu dùng Apps Script)" style="flex:1;min-width:260px;">
      <button class="btn-ok" onclick="saveConfig()">Lưu Link /exec</button>
      <span id="cfg_msg" style="font-size:13px;color:#34d399;"></span>
    </div>
  </div>
  <div class="box">
    <div style="font-weight:700;margin-bottom:8px;">2. Nạp nhanh / Test cộng thẳng vào Game:</div>
    <div class="row">
      <input id="m_pid" type="number" placeholder="UID (VD: 47)" style="width:120px;">
      <select id="m_gid">
        <option value="1">Gói 1 - 3.300 KC (20.000đ - Nạp Đầu)</option>
        <option value="2">Gói 2 - 16.500 KC (60.000đ)</option>
        <option value="3">Gói 3 - 27.500 KC (100.000đ)</option>
        <option value="4">Gói 4 - 55.000 KC (150.000đ)</option>
        <option value="5">Gói 5 - 110.000 KC (200.000đ)</option>
        <option value="6">Gói 6 - 300.000 KC (500.000đ)</option>
        <option value="7">Gói 7 - 650.000 KC (1.000.000đ)</option>
        <option value="8">Gói 8 - 1.350.000 KC (2.000.000đ)</option>
        <option value="9">Gói 9 - Thẻ Tháng (50.000đ)</option>
        <option value="10">Gói 10 - Thẻ Vĩnh Viễn (100.000đ)</option>
        <option value="11">Gói 11 - Gói Ưu Đãi (20.000đ)</option>
        <option value="13">Gói 13 - Quà Đặc Biệt (49.000đ)</option>
      </select>
      <button class="btn-pri" onclick="manualTopup()">Cộng thẳng vào Nick</button>
      <span id="m_msg" style="font-size:13px;"></span>
    </div>
  </div>
  <div class="box">
    <div class="row" style="justify-content:space-between;">
      <div style="font-weight:700;">3. Lịch sử giao dịch SePay & Đơn nạp:</div>
      <button class="btn-pri" onclick="loadOrders()">Làm mới</button>
    </div>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>#</th><th>Thời gian</th><th>Nhân vật (UID)</th><th>Gói</th><th>VNĐ</th><th>Nội dung CK</th><th>Trạng thái</th><th>Hành động</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
<script>
document.getElementById('wh_url').textContent = window.location.origin + '/pay/sepay_webhook';
const PACKS = {1:[6,66],2:[30,330],3:[50,550],4:[100,1100],5:[200,2200],6:[500,6000],7:[1000,13000],8:[2000,27000],9:[30,0],10:[98,0],11:[1,0],13:[49,0]};
async function loadConfig(){const r=await fetch('/pay/config');const d=await r.json();document.getElementById('exec_url').value=d.exec_url||'';}
async function saveConfig(){const exec_url=document.getElementById('exec_url').value.trim();const r=await fetch('/pay/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({exec_url})});const d=await r.json();document.getElementById('cfg_msg').textContent=d.status===0?'✅ Đã lưu!':'Lỗi!';}
async function copyGas(){const r=await fetch('/pay/code_gs');await navigator.clipboard.writeText(await r.text());alert('Đã copy Code.gs!');}
async function loadOrders(){const r=await fetch('/pay/list');const data=await r.json();const tb=document.getElementById('tbody');tb.innerHTML='';for(const o of (data.orders||[])){const stCls='st-'+o.status;const stTxt=o.status==='pending'?'Chờ tiền về':(o.status==='approved'?'✅ Đã cộng':'Đã hủy');const act=o.status==='pending'?`<button class="btn-ok" onclick="act(${o.id},'approve')">Duyệt tay</button> <button class="btn-no" onclick="act(${o.id},'reject')">Hủy</button>`:'-';tb.innerHTML+=`<tr><td>${o.id}</td><td>${o.time}</td><td><b>${o.playerName}</b> (UID:${o.pid})</td><td>${o.goodsName}</td><td><b>${Number(o.vnd).toLocaleString('vi-VN')}đ</b></td><td>${o.note}</td><td class="${stCls}">${stTxt}</td><td>${act}</td></tr>`;}}
async function act(id,type){const r=await fetch('/pay/'+type,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});const j=await r.json();if(j.status!==0)alert(j.message||'Lỗi');loadOrders();}
async function manualTopup(){const pid=Number(document.getElementById('m_pid').value);const gid=Number(document.getElementById('m_gid').value);const msg=document.getElementById('m_msg');if(!pid){msg.textContent='Nhập UID!';return;}const p=PACKS[gid]||[6,66];msg.textContent='Đang cộng...';const r=await fetch('/pay/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid:1,pid,goodsid:gid,rmb:p[0],diamond:p[1]})});const j=await r.json();msg.textContent=j.status===0?'✅ Đã cộng thành công vào UID '+pid+'!':('Lỗi: '+(j.message||''));loadOrders();}
loadConfig();loadOrders();setInterval(loadOrders,4000);
</script>
</body>
</html>
"""


_BACKEND_READY = False

def is_backend_ready():
    global _BACKEND_READY
    if _BACKEND_READY:
        return True
    ready_file = os.environ.get("PKM_READY_FILE", "")
    if ready_file and os.path.exists(ready_file):
        _BACKEND_READY = True
        return True
    try:
        for pt in (18000, 18001, 18002):
            with socket.create_connection(("127.0.0.1", pt), timeout=0.5):
                pass
        _BACKEND_READY = True
        return True
    except Exception:
        return False


class UnifiedProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 75

    def log_message(self, format, *args):
        pass

    def _send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _serve_file_from_root(self, root_dir, rel_path):
        if self.command not in ("GET", "HEAD"):
            return False
        rel = urllib.parse.unquote(rel_path or "/")
        if rel == "/" or rel == "":
            rel = "/index.html"
        safe_rel = os.path.normpath(rel.lstrip("/\\"))
        if safe_rel.startswith(".."):
            return False
        full_path = os.path.join(root_dir, safe_rel)
        if not os.path.isfile(full_path):
            return False

        try:
            st = os.stat(full_path)
            ext = os.path.splitext(full_path)[1].lower()
            ctype = MIME_MAP.get(ext) or mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'

            inm = self.headers.get("If-None-Match")
            if inm and inm.strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                return True

            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes=") and ext in (".mp4", ".mp3", ".ogg", ".wav"):
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                if m:
                    total = st.st_size
                    start = int(m.group(1)) if m.group(1) else 0
                    end = int(m.group(2)) if m.group(2) else (total - 1)
                    start = max(0, min(start, total - 1))
                    end = max(start, min(end, total - 1))
                    length = end - start + 1
                    with open(full_path, "rb") as f:
                        f.seek(start)
                        chunk = f.read(length)
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(chunk)
                    return True

            # Stream static file in 64KB chunks (zero RAM spike, Render Cloudflare CDN handles Brotli/Gzip)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("ETag", etag)
            if ext in (".html", ".htm", ".json", ".js"):
                self.send_header("Cache-Control", "no-cache")
            else:
                self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(st.st_size))
            self.end_headers()
            if self.command != "HEAD":
                with open(full_path, "rb") as f:
                    while True:
                        buf = f.read(65536)
                        if not buf:
                            break
                        self.wfile.write(buf)
            return True
        except Exception:
            return False

    def _handle_pay(self, body):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        qs = urllib.parse.parse_qs(parsed_url.query)

        if path == "/pay/ping":
            self._send_json(200, {"status": 0, "ok": True, "ready": is_backend_ready(), "ts": int(time.time())})
            return True

        if path == "/pay/diag" and (qs.get("key") or [""])[0] == SEPAY_SECRET:
            import glob
            ports = subprocess.run(["ss", "-lnt"], capture_output=True, text=True).stdout
            procs = subprocess.run(["ps", "-eo", "pid,rss,cmd"], capture_output=True, text=True).stdout
            logs = {}
            for lf in sorted(glob.glob("/tmp/*.log")):
                try:
                    logs[os.path.basename(lf)] = open(lf, encoding="utf-8", errors="ignore").read()[-1800:]
                except Exception:
                    pass
            self._send_json(200, {"ready": is_backend_ready(), "ports": ports, "procs": procs, "logs": logs})
            return True

        if path == "/pay/admin" and self.command == "GET":
            raw = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return True

        if path == "/pay/code_gs" and self.command == "GET":
            txt = open(GAS_CODE_FILE, "r", encoding="utf-8").read() if os.path.exists(GAS_CODE_FILE) else ""
            raw = txt.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return True

        if path == "/pay/config":
            if self.command == "GET":
                self._send_json(200, load_json(CONFIG_FILE, {"exec_url": ""}))
                return True
            if self.command == "POST":
                data = json.loads((body or b"{}").decode("utf-8"))
                cfg = load_json(CONFIG_FILE, {})
                cfg["exec_url"] = str(data.get("exec_url", "")).strip()
                save_json(CONFIG_FILE, cfg)
                self._send_json(200, {"status": 0})
                return True

        if path == "/pay/fc7_status" and self.command == "GET":
            pid = int((qs.get("pid") or ["0"])[0])
            self._send_json(200, fc7_get_status(pid))
            return True

        if path == "/pay/fc7_claim" and self.command == "POST":
            try:
                data = json.loads((body or b"{}").decode("utf-8"))
                out = fc7_claim_day(data.get("pid", 0), data.get("day", 0), data.get("choice", 0))
                self._send_json(200, out)
            except Exception as e:
                self._send_json(500, {"status": -1, "message": str(e)})
            return True

        if path == "/pay/check_status" and self.command == "GET":
            pid = int((qs.get("pid") or ["0"])[0])
            goodsid = int((qs.get("goodsid") or ["0"])[0])
            since = int((qs.get("since") or ["0"])[0])
            orders = load_orders()
            paid = any(
                o.get("status") == "approved"
                and int(o.get("pid", 0)) == pid
                and int(o.get("goodsid", 0)) == goodsid
                and int(o.get("ts", 0)) >= since
                for o in orders
            )
            self._send_json(200, {"status": 0, "paid": paid})
            return True

        if path == "/pay/sepay_webhook" and self.command == "POST":
            try:
                data = json.loads((body or b"{}").decode("utf-8"))
                if str(data.get("transferType", "in")).lower() != "in":
                    self._send_json(200, {"success": True, "ignored": "not_in"})
                    return True
                content = str(data.get("content") or data.get("description") or "")
                amount = int(data.get("transferAmount") or data.get("amount") or 0)
                tx_id = str(data.get("id") or data.get("referenceCode") or int(time.time() * 1000))
                m = re.search(r"PKM\s*(\d+)\s*G\s*(\d+)", content, re.I)
                if not m:
                    self._send_json(200, {"success": True, "ignored": "no_pkm_prefix"})
                    return True
                res = process_sepay_transaction(tx_id, int(m.group(1)), int(m.group(2)), amount, content, source="SePayWebhook")
                self._send_json(200, {"success": True, "result": res})
            except Exception as e:
                self._send_json(200, {"success": False, "error": str(e)})
            return True

        if path == "/pay/list" and self.command == "GET":
            self._send_json(200, {"status": 0, "orders": list(reversed(load_orders()))})
            return True

        if path == "/pay/create" and self.command == "POST":
            data = json.loads((body or b"{}").decode("utf-8"))
            orders = load_orders()
            new_id = (orders[-1]["id"] + 1) if orders else 1
            orders.append({
                "id": new_id, "ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sid": int(data.get("sid", 1)), "pid": int(data.get("pid", 0)),
                "playerName": str(data.get("playerName", "")), "goodsid": int(data.get("goodsid", 1)),
                "goodsName": str(data.get("goodsName", "")), "rmb": int(data.get("rmb", 6)),
                "diamond": int(data.get("diamond", 0)), "vnd": int(data.get("vnd", 6000)),
                "note": str(data.get("note", "")), "status": "pending",
            })
            save_orders(orders)
            self._send_json(200, {"status": 0, "orderId": new_id})
            return True

        if path == "/pay/approve" and self.command == "POST":
            data = json.loads((body or b"{}").decode("utf-8"))
            oid = int(data.get("id", 0))
            orders = load_orders()
            target = next((o for o in orders if o["id"] == oid), None)
            if not target:
                self._send_json(404, {"status": -1, "message": "Không tìm thấy đơn"})
                return True
            if target["status"] != "pending":
                self._send_json(400, {"status": -1, "message": "Đơn đã xử lý rồi"})
                return True
            try:
                res = gm_login_and_recharge(target["sid"], target["pid"], target["goodsid"], target["rmb"], target["diamond"])
                if res.get("status") == 0 and res.get("result"):
                    target["status"] = "approved"
                    target["ts"] = int(time.time())
                    save_orders(orders)
                    try:
                        fc7_record_first_charge(target["pid"])
                    except Exception:
                        pass
                    self._send_json(200, {"status": 0})
                else:
                    self._send_json(500, {"status": -1, "message": str(res)})
            except Exception as e:
                self._send_json(500, {"status": -1, "message": str(e)})
            return True

        if path == "/pay/reject" and self.command == "POST":
            data = json.loads((body or b"{}").decode("utf-8"))
            oid = int(data.get("id", 0))
            orders = load_orders()
            for o in orders:
                if o["id"] == oid and o["status"] == "pending":
                    o["status"] = "rejected"
            save_orders(orders)
            self._send_json(200, {"status": 0})
            return True

        if path == "/pay/manual" and self.command == "POST":
            data = json.loads((body or b"{}").decode("utf-8"))
            try:
                pid, gid = int(data.get("pid", 0)), int(data.get("goodsid", 1))
                rmb, dia = int(data.get("rmb", 6)), int(data.get("diamond", 66))
                res = gm_login_and_recharge(int(data.get("sid", 1)), pid, gid, rmb, dia)
                if res.get("status") == 0 and res.get("result"):
                    orders = load_orders()
                    new_id = (orders[-1]["id"] + 1) if orders else 1
                    orders.append({
                        "id": new_id, "ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "sid": 1, "pid": pid, "playerName": f"UID:{pid} (Admin Buff)",
                        "goodsid": gid, "goodsName": f"Gói #{gid} ({rmb} Tệ)",
                        "rmb": rmb, "diamond": dia, "vnd": get_expected_vnd(gid, rmb),
                        "note": "MANUAL_ADMIN", "status": "approved",
                    })
                    save_orders(orders)
                    try:
                        fc7_record_first_charge(pid)
                    except Exception:
                        pass
                    self._send_json(200, {"status": 0})
                else:
                    self._send_json(500, {"status": -1, "message": str(res)})
            except Exception as e:
                self._send_json(500, {"status": -1, "message": str(e)})
            return True

        return False

    def _route(self, path):
        if path.startswith("/api8000/"):
            return "127.0.0.1", 18000, path[len("/api8000"):]
        if path == "/api8000":
            return "127.0.0.1", 18000, "/"
        if path.startswith("/agent/") or path.startswith("/agent?"):
            return "127.0.0.1", 18001, path
        if path.startswith("/gm/"):
            return "127.0.0.1", 18002, path[len("/gm"):]
        ref = self.headers.get("Referer") or ""
        if "/gm" in ref or any(path.startswith(p) for p in GM_API_PREFIXES):
            return "127.0.0.1", 18002, path
        return "127.0.0.1", 18080, path

    def _proxy(self):
        if self.command == "OPTIONS":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        content_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_len) if content_len > 0 else None
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if not is_backend_ready() and not p.startswith("/pay/"):
            boot_html = (
                "<!DOCTYPE html><html lang='vi'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<meta http-equiv='refresh' content='4'><title>Pixel Pokemon H5 - Đang khởi động</title>"
                "<style>body{background:#0f172a;color:#f8fafc;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;}"
                ".card{background:#1e293b;padding:28px 32px;border-radius:16px;border:1px solid #334155;max-width:360px;}"
                "h2{color:#fbbf24;margin:0 0 10px;}p{color:#cbd5e1;font-size:14px;line-height:1.5;}</style></head>"
                "<body><div class='card'><h2>⚡ Pixel Pokemon H5</h2>"
                "<p>Máy chủ Cloud đang thức dậy (~15 giây)...<br>Trang sẽ tự động vào game ngay khi sẵn sàng!</p></div></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(boot_html)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(boot_html)
            return

        # Redirect /gm -> /gm/ so relative scripts/css resolve cleanly
        if p == "/gm":
            self.send_response(301)
            self.send_header("Location", "/gm/")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if p.startswith("/pay/"):
            if self._handle_pay(body):
                return

        # Serve GM Panel static assets from /gm/...
        if p.startswith("/gm/"):
            if self._serve_file_from_root(GM_ROOT, p[len("/gm"):]):
                return

        # Serve Game Client static files directly
        ref = self.headers.get("Referer") or ""
        is_api = (
            p.startswith("/api8000")
            or p.startswith("/agent/")
            or p.startswith("/agent?")
            or p.startswith("/proxy")
            or p.startswith("/gm/")
            or any(p.startswith(x) for x in GM_API_PREFIXES)
        )
        if not is_api:
            if "/gm" in ref and self._serve_file_from_root(GM_ROOT, p):
                return
            if self._serve_file_from_root(CLIENT_ROOT, p):
                return
            if (
                p.startswith(("/resource/", "/icon/", "/assets/", "/ui/", "/anim/", "/langs/", "/sound/", "/particle/", "/bones/", "/js/", "/css/"))
                or os.path.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".json", ".js", ".css", ".mp3", ".wav", ".fnt", ".exml", ".ico")
            ):
                self._send_json(404, {"error": "Not Found", "path": p})
                return

        target_host, target_port, target_path = self._route(self.path)
        fwd_headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP_BY_HOP or kl == "host" or kl == "accept-encoding":
                continue
            fwd_headers[k] = v
        fwd_headers["Host"] = f"127.0.0.1:{target_port}"
        fwd_headers["Connection"] = "close"

        last_err = None
        for attempt in range(2):
            conn = None
            try:
                conn = http.client.HTTPConnection(target_host, target_port, timeout=70)
                conn.request(self.command, target_path, body=body, headers=fwd_headers)
                resp = conn.getresponse()
                data = resp.read()

                self.send_response(resp.status, resp.reason)
                has_cors = False
                for k, v in resp.getheaders():
                    kl = k.lower()
                    if kl in HOP_BY_HOP or kl == "content-length":
                        continue
                    if kl == "access-control-allow-origin":
                        has_cors = True
                    self.send_header(k, v)
                if not has_cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            except Exception as e:
                last_err = e
                if attempt == 0 and "/notify" not in target_path:
                    time.sleep(0.25)
                    continue
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        err_body = f"Proxy error: {last_err}".encode("utf-8", errors="ignore")
        try:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception:
            pass

    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_HEAD(self): self._proxy()
    def do_OPTIONS(self): self._proxy()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512


def backup_periodic_loop():
    time.sleep(90)
    interval = int(os.environ.get("BACKUP_INTERVAL_SEC", "300"))
    while True:
        try:
            if is_backend_ready():
                subprocess.run(["python3", "/app/backup_sync.py", "once"], timeout=90)
        except Exception as e:
            print(f"[backup] periodic err: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    print(f"Starting Unified Cloud Proxy on 0.0.0.0:{LISTEN_PORT} ...", flush=True)
    threading.Thread(target=poll_apps_script_loop, daemon=True).start()
    threading.Thread(target=keep_services_warm_loop, daemon=True).start()
    threading.Thread(target=backup_periodic_loop, daemon=True).start()
    while True:
        try:
            srv = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), UnifiedProxyHandler)
            srv.serve_forever()
        except Exception as e:
            print(f"Proxy error: {e}", flush=True)
            time.sleep(2)
