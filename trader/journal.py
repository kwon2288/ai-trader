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
"""


def now() -> datetime:
    return datetime.now(KST)


class Journal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:  # 예전 DB 호환: 어떤 AI가 판단했는지 기록하는 칸 추가
            self.db.execute("ALTER TABLE runs ADD COLUMN ai TEXT")
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

    def add_decision(self, run_id: int, d: dict, outcome: str) -> None:
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)", (
            run_id, d.get("code"), d.get("action"), d.get("amount_krw"), d.get("sell_ratio"),
            d.get("confidence"), d.get("reason"), outcome))
        self.db.commit()

    def add_order(self, mode, side, code, name, qty, est_price, res, trigger) -> None:
        self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            now().isoformat(timespec="seconds"), mode, side, code, name, qty, est_price,
            res.order_no, int(res.ok), res.message, trigger))
        self.db.commit()

    def orders_today(self, mode: str) -> int:
        today = now().date().isoformat()
        row = self.db.execute("SELECT COUNT(*) c FROM orders WHERE ok=1 AND mode=? AND ts LIKE ?",
                              (mode, today + "%")).fetchone()
        return row["c"]

    def recent_decisions(self, limit: int = 12) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.ts, d.code, d.action, d.confidence, d.reason, d.outcome FROM decisions d "
            "JOIN runs r ON r.id=d.run_id ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # 봇 보유 종목
    def bot_codes(self) -> dict[str, str]:
        return {r["code"]: r["opened"] for r in self.db.execute("SELECT * FROM bot_positions")}

    def open_position(self, code: str, name: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO bot_positions VALUES(?,?,?)",
                        (code, name, now().date().isoformat()))
        self.db.commit()

    def close_position(self, code: str) -> None:
        self.db.execute("DELETE FROM bot_positions WHERE code=?", (code,))
        self.db.commit()

    def held_days(self, code: str) -> int:
        opened = self.bot_codes().get(code)
        return (now().date() - date.fromisoformat(opened)).days if opened else 999

    # 웹 화면용
    def save_snapshot(self, mode: str, snap: dict) -> None:
        snap = {**snap, "ts": now().isoformat(timespec="seconds")}
        self.set(f"snapshot_{mode}", json.dumps(snap, ensure_ascii=False))

    def snapshot(self, mode: str) -> dict | None:
        v = self.get(f"snapshot_{mode}")
        return json.loads(v) if v else None

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
            r["decisions"] = [dict(d) for d in self.db.execute(
                "SELECT code, action, confidence, reason, outcome FROM decisions WHERE run_id=?", (r["id"],))]
        return runs

    def orders(self, mode: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM orders WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit))]

    def report(self, days: int = 30) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, mode, side, code, name, qty, est_price, ok, trigger, message FROM orders "
            "WHERE ts >= date('now', ?) ORDER BY ts", (f"-{days} day",)).fetchall()
        return [dict(r) for r in rows]
