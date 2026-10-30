"""매매 일지 (SQLite). 봇이 산 종목만 봇이 판다 — 계좌의 다른 보유 주식은 건드리지 않음."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, market_view TEXT, equity INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  run_id INTEGER, code TEXT, action TEXT, amount_krw INTEGER, sell_ratio REAL,
  confidence REAL, reason TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS orders(
  ts TEXT, mode TEXT, side TEXT, code TEXT, name TEXT, qty INTEGER, est_price INTEGER,
  order_no TEXT, ok INTEGER, message TEXT, trigger TEXT);
CREATE TABLE IF NOT EXISTS bot_positions(code TEXT PRIMARY KEY, name TEXT, opened TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS run_logs(ts TEXT, mode TEXT, kind TEXT, source TEXT, output TEXT);
CREATE TABLE IF NOT EXISTS ai_usage(ts TEXT, ledger TEXT, provider TEXT, model TEXT, tin INTEGER, tout INTEGER,
  source TEXT);
"""


def now() -> datetime:
    return datetime.now(KST)


class Journal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # 예전 DB 호환: 나중에 추가된 칸
        for sql in ("ALTER TABLE runs ADD COLUMN ai TEXT",                       # 어떤 AI가 판단했는지
                    "ALTER TABLE bot_positions ADD COLUMN market TEXT DEFAULT 'kr'",
                    "ALTER TABLE decisions ADD COLUMN name TEXT",
                    "ALTER TABLE bot_positions ADD COLUMN qty INTEGER",                # 봇이 산 수량
                    "ALTER TABLE bot_positions ADD COLUMN avg REAL",
                    # 체결 확인 (v0.14): 실제 체결 수량·평균가·상태·수수료·세금
                    "ALTER TABLE orders ADD COLUMN status TEXT",
                    "ALTER TABLE orders ADD COLUMN filled_qty REAL",
                    "ALTER TABLE orders ADD COLUMN fill_price REAL",
                    "ALTER TABLE orders ADD COLUMN fee REAL",
                    "ALTER TABLE orders ADD COLUMN tax REAL"):                   # 봇 평단                    # 종목명  # 국내/미국
            try:
                self.db.execute(sql)
                self.db.commit()
            except sqlite3.OperationalError:
                pass

    # 상태
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, value))
        self.db.commit()

    # 실행 기록
    def start_run(self, mode: str, equity: int) -> int:
        cur = self.db.execute("INSERT INTO runs(ts,mode,equity) VALUES(?,?,?)",
                              (now().isoformat(timespec="seconds"), mode, equity))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, market_view: str, raw: dict) -> None:
        self.db.execute("UPDATE runs SET market_view=?, raw=?, ai=? WHERE id=?",
                        (market_view, json.dumps(raw, ensure_ascii=False), raw.get("_provider"), run_id))
        self.db.commit()

    def add_decision(self, run_id: int, d: dict, outcome: str, name: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO decisions(run_id,code,action,amount_krw,sell_ratio,confidence,reason,outcome,name) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (
                run_id, d.get("code"), d.get("action"), d.get("amount_krw"), d.get("sell_ratio"),
                d.get("confidence"), d.get("reason"), outcome, name))
        self.db.commit()

    # 종목명 사전 (코드 → 이름). 예전 기록처럼 이름이 없는 판단도 이름으로 보여 주려고 쓴다.
    def names(self) -> dict[str, str]:
        try:
            return json.loads(self.get("names", "{}") or "{}")
        except ValueError:
            return {}

    def remember_names(self, mapping: dict[str, str]) -> None:
        cur = self.names()
        new = {c: n for c, n in mapping.items() if c and n and n != c and cur.get(c) != n}
        if new:
            cur.update(new)
            self.set("names", json.dumps(cur, ensure_ascii=False))

    def _fill_names(self, rows: list[dict]) -> list[dict]:
        nm = None
        for r in rows:
            if not r.get("name"):
                nm = nm if nm is not None else self.names()
                r["name"] = nm.get(r.get("code") or "", "")
        return rows

    def add_order(self, mode, side, code, name, qty, est_price, res, trigger, status: str | None = None) -> int:
        """status: sent(체결 확인 대기) · filled(체결로 간주) · failed. 반환: rowid"""
        if status is None:
            status = "failed" if not res.ok else ("filled" if res.order_no == "DRY" else "sent")
        filled = qty if status == "filled" else 0
        cur = self.db.execute(
            "INSERT INTO orders(ts,mode,side,code,name,qty,est_price,order_no,ok,message,trigger,status,filled_qty,"
            "fill_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                now().isoformat(timespec="seconds"), mode, side, code, name, qty, est_price,
                res.order_no, int(res.ok), res.message, trigger, status, filled, est_price if filled else None))
        self.db.commit()
        return cur.lastrowid

    def open_orders(self, mode: str) -> list[dict]:
        return [dict(r, rowid=r["rowid"]) for r in self.db.execute(
            "SELECT rowid, * FROM orders WHERE mode=? AND ok=1 AND status IN ('sent','open','partial') ORDER BY ts",
            (mode,))]

    def update_fill(self, rowid: int, status: str, filled: float, price: float | None,
                    fee: float | None = None, tax: float | None = None) -> None:
        self.db.execute("UPDATE orders SET status=?, filled_qty=?, fill_price=COALESCE(?, fill_price), "
                        "fee=COALESCE(?, fee), tax=COALESCE(?, tax) WHERE rowid=?",
                        (status, filled, price or None, fee, tax, rowid))
        self.db.commit()

    def orders_today(self, mode: str) -> int:
        today = now().date().isoformat()
        row = self.db.execute("SELECT COUNT(*) c FROM orders WHERE ok=1 AND mode=? AND ts LIKE ?",
                              (mode, today + "%")).fetchone()
        return row["c"]

    def recent_decisions(self, limit: int = 12, mode: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.ts, d.code, d.name, d.action, d.confidence, d.reason, d.outcome FROM decisions d "
            "JOIN runs r ON r.id=d.run_id WHERE (? IS NULL OR r.mode=?) ORDER BY r.id DESC LIMIT ?",
            (mode, mode, limit)).fetchall()
        return self._fill_names([dict(r) for r in rows])

    # 봇 보유 종목
    def bot_codes(self, market: str = "kr") -> dict[str, str]:
        return {r["code"]: r["opened"] for r in self.db.execute(
            "SELECT * FROM bot_positions WHERE COALESCE(market,'kr')=?", (market,))}

    def open_position(self, code: str, name: str, market: str = "kr",
                      qty: int | None = None, price: float | None = None) -> None:
        row = self.db.execute("SELECT qty, avg FROM bot_positions WHERE code=?", (code,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO bot_positions(code,name,opened,market,qty,avg) VALUES(?,?,?,?,?,?)",
                            (code, name, now().date().isoformat(), market, qty, price))
        elif qty and row["qty"] is not None:
            q0, a0 = row["qty"], row["avg"] or price or 0
            q1 = q0 + qty
            self.db.execute("UPDATE bot_positions SET qty=?, avg=? WHERE code=?",
                            (q1, round((q0 * a0 + qty * (price or a0)) / q1, 4), code))
        self.db.commit()

    def reduce_position(self, code: str, qty: int) -> None:
        """봇이 판 만큼 줄이고, 다 팔면 장부에서 지운다. 수량을 모르는 예전 기록은 그냥 지운다."""
        row = self.db.execute("SELECT qty FROM bot_positions WHERE code=?", (code,)).fetchone()
        if row is None:
            return
        if row["qty"] is None or row["qty"] - qty <= 0:
            self.close_position(code)
        else:
            self.db.execute("UPDATE bot_positions SET qty=? WHERE code=?", (row["qty"] - qty, code))
            self.db.commit()

    def bot_lots(self, market: str = "kr", ledger: str | None = None) -> dict[str, tuple]:
        """봇 보유 종목 → (봇 수량 또는 None, 봇 평단 또는 None).
        수량을 기록하기 전(v0.6.2 이전)에 산 종목은 주문 기록에서 봇 수량·평단을 계산해 채운다."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT code, opened, qty, avg FROM bot_positions WHERE COALESCE(market,'kr')=?", (market,))]
        for r in rows:
            if r["qty"] is None and ledger:
                q, a = self._lot_from_orders(r["code"], ledger, r["opened"])
                if q > 0:
                    self.db.execute("UPDATE bot_positions SET qty=?, avg=? WHERE code=?", (q, a, r["code"]))
                    self.db.commit()
                    r["qty"], r["avg"] = q, a
        return {r["code"]: (r["qty"], r["avg"]) for r in rows}

    def _lot_from_orders(self, code: str, ledger: str, opened: str) -> tuple[int, float]:
        qty, cost = 0, 0.0
        for o in self.db.execute(
                "SELECT side, qty, est_price FROM orders WHERE ok=1 AND mode=? AND code=? AND ts >= ? "
                "AND COALESCE(order_no,'') != 'DRY' ORDER BY ts", (ledger, code, opened or "")):
            if o["side"] == "BUY":
                qty, cost = qty + o["qty"], cost + o["qty"] * o["est_price"]
            else:
                avg = cost / qty if qty else 0
                qty = max(0, qty - o["qty"])
                cost = avg * qty
        return qty, (round(cost / qty, 4) if qty else 0.0)

    def net_spent(self, ledger: str) -> float:
        """봇이 지금까지 쓴 돈(매수 - 매도). 예산 변경을 장부에 반영할 때 쓴다."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN qty*est_price ELSE -qty*est_price END),0) s "
            "FROM orders WHERE ok=1 AND mode=? AND COALESCE(order_no,'') != 'DRY'", (ledger,)).fetchone()
        return float(row["s"] or 0)

    def close_position(self, code: str) -> None:
        self.db.execute("DELETE FROM bot_positions WHERE code=?", (code,))
        self.db.commit()

    def held_days(self, code: str) -> int:
        row = self.db.execute("SELECT opened FROM bot_positions WHERE code=?", (code,)).fetchone()
        opened = row["opened"] if row else None
        return (now().date() - date.fromisoformat(opened)).days if opened else 999

    # 웹 화면용
    def save_snapshot(self, mode: str, snap: dict) -> None:
        snap = {**snap, "ts": now().isoformat(timespec="seconds")}
        self.set(f"snapshot_{mode}", json.dumps(snap, ensure_ascii=False))

    def snapshot(self, mode: str) -> dict | None:
        v = self.get(f"snapshot_{mode}")
        return json.loads(v) if v else None

    # AI 토큰 사용량
    def add_usage(self, ledger: str, provider: str, model: str, tin: int, tout: int, source: str) -> None:
        self.db.execute("INSERT INTO ai_usage VALUES(?,?,?,?,?,?,?)",
                        (now().isoformat(timespec="seconds"), ledger, provider, model, tin, tout, source))
        self.db.commit()

    def usage(self, since: str | None = None) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM ai_usage WHERE (? IS NULL OR ts >= ?) ORDER BY ts", (since, since))]

    def add_log(self, mode: str, kind: str, source: str, output: str) -> None:
        self.db.execute("INSERT INTO run_logs VALUES(?,?,?,?,?)",
                        (now().isoformat(timespec="seconds"), mode, kind, source, output))
        self.db.execute("DELETE FROM run_logs WHERE rowid NOT IN "
                        "(SELECT rowid FROM run_logs ORDER BY rowid DESC LIMIT 300)")
        self.db.commit()

    def logs(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT rowid id, * FROM run_logs ORDER BY rowid DESC LIMIT ?", (limit,))]

    def equity_series(self, mode: str, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT ts, equity FROM runs WHERE mode=? AND equity IS NOT NULL "
                               "ORDER BY id DESC LIMIT ?", (mode, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def runs_with_decisions(self, mode: str, limit: int = 10) -> list[dict]:
        runs = [dict(r) for r in self.db.execute(
            "SELECT id, ts, market_view, equity, ai FROM runs WHERE mode=? AND market_view IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (mode, limit))]
        for r in runs:
            r["decisions"] = self._fill_names([dict(d) for d in self.db.execute(
                "SELECT code, name, action, confidence, reason, outcome FROM decisions WHERE run_id=?", (r["id"],))])
        return runs

    def orders(self, mode: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM orders WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit))]

    def report(self, days: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, mode, side, code, name, qty, est_price, ok, trigger, message FROM orders "
            "WHERE ts >= date('now', ?) ORDER BY ts", (f"-{days} day",)).fetchall()
        return [dict(r) for r in rows]
