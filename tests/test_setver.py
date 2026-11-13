"""설정 버전별 · 매도 사유별 손익."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import ledger, setver  # noqa: E402
from trader.bot import Bot  # noqa: E402
from trader.journal import Journal  # noqa: E402
import test_bot as tb  # noqa: E402

OK = SimpleNamespace(ok=True, order_no="1", message="")


def test_exit_reason():
    assert setver.exit_reason("손절(장중 감시)") == "손절" and setver.exit_reason("익절") == "익절"
    assert setver.exit_reason("이익 보호") == "이익 보호" and setver.exit_reason("보유기간 만료") == "보유기간 만료"
    assert setver.exit_reason("규칙 매도: 돌파 실패: 돌파선 10,000 아래") == "돌파 실패"
    assert setver.exit_reason("규칙 매도: 10일 최저가 이탈") == "규칙 매도 신호"
    assert setver.exit_reason("AI") == "AI 매도" and setver.exit_reason(None) == "직접·기타"


def test_versions_change_only_when_strategy_changes():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    j = Journal(cfg.db_path)
    v1 = setver.current(j, cfg)
    assert setver.current(j, cfg) == v1                                      # 그대로면 같은 버전
    cfg.total_krw = 999_999                                                  # 예산은 전략이 아님
    assert setver.current(j, cfg) == v1
    cfg.stop_loss_pct = -5
    v2 = setver.current(j, cfg)
    cfg.strategy = {"signal": "dip", "stop_atr": 2}
    cfg.ai_hybrid = True
    v3 = setver.current(j, cfg)
    vs = setver.all_for(j, cfg.ledger)
    assert [vs[v]["no"] for v in (v1, v2, v3)] == [1, 2, 3]
    assert vs[v2]["changed"] == ["손절"] and set(vs[v3]["changed"]) == {"매매 신호", "ATR 손절", "규칙 필터"}
    assert "눌림목 반등" in vs[v3]["label"] and "2×ATR" in vs[v3]["label"] and vs[v3]["entry"] == "hybrid"
    other = tb.make_cfg(Path(tempfile.mkdtemp()), mode="dry")               # 장부마다 따로
    assert setver.current(Journal(other.db_path), other) == 1


def test_trades_grouped_by_entry_version_and_exit():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()))
    cfg.costs = {"kr_fee_pct": 0, "kr_sell_tax_pct": 0}
    j = Journal(cfg.db_path)
    L = cfg.ledger
    j.setver[L] = setver.current(j, cfg)
    j.add_order(L, "BUY", "000001", "가", 2, 10_000, OK, "AI", "filled")          # v1 에 삼
    j.add_order(L, "BUY", "000002", "나", 2, 10_000, OK, "AI", "filled")
    j.add_order(L, "SELL", "000002", "나", 2, 9_300, OK, "손절", "filled")
    cfg.stop_loss_pct = -5
    j.setver[L] = setver.current(j, cfg)                                       # v2 로 바뀐 뒤 팔아도
    j.add_order(L, "SELL", "000001", "가", 2, 10_800, OK, "익절", "filled")       # 산 버전(v1) 으로 묶임
    j.add_order(L, "BUY", "000003", "다", 1, 10_000, OK, "AI", "filled")
    j.add_order(L, "SELL", "000003", "다", 1, 9_600, OK, "규칙 매도: 돌파 실패: x", "filled")
    j.setver.pop(L)
    j.add_order(L, "BUY", "000004", "라", 1, 10_000, OK, "", "filled")            # 버전 모름 (직접)
    j.add_order(L, "SELL", "000004", "라", 1, 10_100, OK, "", "filled")
    r = ledger.period(j, L, None, None, ledger.costs_of(cfg))
    byv = {g["key"]: g for g in r["by_version"]}
    assert byv[1]["n"] == 2 and byv[1]["pnl"] == 1_600 - 1_400 and byv[1]["wins"] == 1
    assert byv[2]["n"] == 1 and byv[2]["pnl"] == -400 and byv[None]["n"] == 1
    assert {g["key"]: g["n"] for g in r["by_exit"]} == {"익절": 1, "손절": 1, "돌파 실패": 1, "직접·기타": 1}
    assert sum(g["pnl"] for g in r["by_version"]) == r["pnl"] == sum(g["pnl"] for g in r["by_exit"])


def test_bot_tags_orders():
    cfg = tb.make_cfg(Path(tempfile.mkdtemp()), mode="dry")
    b = Bot(cfg, tb.FakeBroker({"000001": 10_000}), None, Journal(cfg.db_path))
    assert b.j.setver.get(cfg.ledger) == 1
    b.j.add_order(cfg.ledger, "BUY", "000001", "가", 1, 10_000, OK, "AI", "filled")
    assert b.j.db.execute("SELECT setver FROM orders").fetchone()[0] == 1


def test_pnl_page_shows_attribution():
    os.environ.update(TRADER_DATA_DIR=tempfile.mkdtemp(), WEB_PASSWORD="secret-pass", TRADING_MODE="dry")
    from web import app as W
    c = W.cfg()
    j = Journal(c.db_path)
    j.setver[c.ledger] = setver.current(j, c)
    j.add_order(c.ledger, "BUY", "000001", "가", 2, 10_000, SimpleNamespace(ok=True, order_no="X", message=""), "AI", "filled")
    j.add_order(c.ledger, "SELL", "000001", "가", 2, 9_000, SimpleNamespace(ok=True, order_no="X", message=""), "손절", "filled")
    cl = W.app.test_client()
    with cl.session_transaction() as s:
        s["ok"] = True
    h = cl.get("/pnl?m=kr&p=all").get_data(as_text=True)
    assert "설정별 손익" in h and "v1" in h and "지금 설정" in h and "매도 사유별" in h and "손절" in h


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
