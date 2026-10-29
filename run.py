#!/usr/bin/env python3
"""AI 자동매매 실행기.

  python run.py check     # 연결·계좌·잔고 확인 (주문 없음)
  python run.py accounts  # (NH) 계좌 목록 · 국내주식 사용 가능 여부
  python run.py scan      # 자동 선정 종목 지금 다시 찾기 (평소엔 그날 첫 실행 때 자동)
  python run.py aitest    # AI 키·모델 연결 확인 (Claude/Gemini/ChatGPT)
  python run.py analyze   # AI 분석 결과만 보기 (주문 없음, 장외 시간에도 가능)
  python run.py trade     # 실제 사이클 실행 — TRADING_MODE(dry/mock/live)에 따라 동작
  python run.py report    # 최근 30일 주문 내역
  python run.py stop      # 킬스위치 ON (이후 trade 가 아무것도 안 함)
  python run.py resume    # 킬스위치·손실한도 정지 해제
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from trader import config
from trader.journal import Journal, now


def setup_logging(cfg) -> None:
    (cfg.data_dir / "logs").mkdir(exist_ok=True)
    fh = RotatingFileHandler(cfg.data_dir / "logs" / "trader.log", maxBytes=2_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[fh])


def guard_live(cfg) -> None:
    if cfg.mode == "live" and os.environ.get("I_UNDERSTAND_REAL_MONEY") != "yes":
        sys.exit("실계좌 모드입니다. 실제 돈이 움직입니다.\n"
                 ".env 에 I_UNDERSTAND_REAL_MONEY=yes 를 넣어야 실행됩니다.")


def aitest(cfg) -> None:
    """키가 있는 AI 마다 작은 가짜 데이터로 한 번씩 물어 연결·모델명을 확인 (주문·시세 조회 없음)."""
    import time
    from trader.analyst import PROVIDERS, _CLASSES, available
    ctx = {"today": "test", "rules": {"max_per_position_krw": 50000},
           "portfolio": {"cash_krw": 100000, "positions": []},
           "candidates": [{"code": "069500", "name": "KODEX 200", "price": 40000, "rsi14": 52,
                           "ma5_above_ma20": True, "ret_20d_pct": 3.1, "warnings": []}]}
    for p, info in PROVIDERS.items():
        if not available(p):
            print(f"– {info['label']}: 키 없음 ({info['env']}) — 건너뜀")
            continue
        t = time.time()
        try:
            r = _CLASSES[p](cfg.ai_models[p], 1500).decide(ctx)
            d = r["decisions"][0] if r["decisions"] else {}
            print(f"✅ {r['_provider']}: {time.time() - t:.1f}초 · {d.get('action', '판단없음')} "
                  f"(확신 {d.get('confidence', 0):.2f}) · {(r['market_view'] or '')[:60]}")
        except Exception as e:  # noqa: BLE001
            print(f"❌ {info['label']}({cfg.ai_models[p]}): {e}")


def main() -> None:
    from trader import __version__
    p = argparse.ArgumentParser(description="AI 자동매매")
    p.add_argument("--version", action="version", version=f"ai-trader {__version__}")
    p.add_argument("cmd", choices=["check", "accounts", "scan", "aitest", "analyze", "trade", "report", "stop", "resume",
                                      "passwd"])
    p.add_argument("--force", action="store_true", help="장 상태 확인 무시 (테스트용)")
    p.add_argument("--market", choices=["kr", "us"], default="kr", help="kr=국내(기본), us=미국")
    args = p.parse_args()

    if args.cmd == "passwd":                         # 웹 비밀번호 설정 (.env 가 없으면 예시에서 만든다)
        import getpass
        from trader import envfile
        envfile.ensure()
        pw = getpass.getpass("새 웹 비밀번호 (8자 이상): ")
        if len(pw) < 8 or pw != getpass.getpass("한 번 더: "):
            sys.exit("8자 이상이어야 하고 두 번 같아야 합니다.")
        if envfile.check_value(pw) or " #" in pw:
            sys.exit("따옴표·역슬래시·' #' 은 쓸 수 없습니다.")
        envfile.update({"WEB_PASSWORD": pw})
        print("✅ 저장했습니다. 웹 화면: sudo systemctl restart ai-trader-web\n"
              "   나머지(앱키·AI 키)는 웹 → 설정 → 연결 정보에서 넣을 수 있어요.")
        return

    cfg = config.load()
    setup_logging(cfg)

    if args.cmd == "stop":
        cfg.stop_file.write_text(now().isoformat())
        print("⏸ 킬스위치 ON — 다음 실행부터 매매하지 않습니다.")
        return
    if args.cmd == "resume":
        cfg.stop_file.unlink(missing_ok=True)
        j = Journal(cfg.db_path)
        for m in ("kr", "us"):                       # 국내·미국 손실한도 정지 모두 해제
            j.set(f"halted_{cfg.for_market(m).ledger}", "0")
        print("▶ 정지 해제했습니다.")
        return
    if args.cmd == "report":
        rows = Journal(cfg.db_path).report()
        if not rows:
            print("주문 내역이 없습니다.")
        for r in rows:
            usd = str(r["mode"]).endswith("_us")
            amt = r["qty"] * r["est_price"]
            print(f"{r['ts'][:16]} [{r['mode']}] {'매수' if r['side'] == 'BUY' else '매도'} "
                  f"{r['name']}({r['code']}) {r['qty']}주 ≈{f'${amt:,.2f}' if usd else f'{amt:,.0f}원'} "
                  f"{'OK' if r['ok'] else 'FAIL'} [{r['trigger']}]")
        return

    if args.cmd == "aitest":
        aitest(cfg)
        return

    if args.cmd == "accounts":
        if os.environ.get("BROKER", "nh").lower() == "toss":
            from trader.toss import TossBroker
            b = TossBroker("dry", "kr", cfg.data_dir, account_seq="-")
            for a in b.list_accounts():
                print(f"  계좌 {a['acct_no']}  유형 {a['acct_type']}  accountSeq {a['seq']}")
            print("→ 여러 개면 .env 에 TOSS_ACCOUNT_SEQ=<accountSeq> 로 지정하세요.")
            return
        from trader.broker import NHBroker
        b = NHBroker(cfg.mode, account="-")   # 자동 선택 건너뛰고 목록만
        print(f"서버: {b.env}")
        for a in b.list_accounts():
            print(f"  {a['acct_no']}  구분 {a['acct_type']}  국내주식 {'✅ 사용 가능' if a['stock_ok'] else '❌'}")
        print("→ .env 에 NHPLUG_ACCOUNT=<위 계좌번호> 로 지정하세요.")
        return

    from trader.broker import BrokerError
    from trader.runner import Busy, broker_name, make_broker, market_cfg, run_cycle

    if args.cmd in ("analyze", "trade"):
        if args.cmd == "trade":
            guard_live(cfg)
        try:
            print(run_cycle(cfg, analyze_only=(args.cmd == "analyze"), force=args.force,
                            source="timer" if os.environ.get("INVOCATION_ID") else "cli", market=args.market))
        except Busy as e:
            sys.exit(str(e))
        return

    try:
        cfg = market_cfg(cfg, args.market)
        broker = make_broker(cfg)
    except BrokerError as e:
        sys.exit(f"증권사 연결 설정 오류: {e}")

    if args.cmd == "scan":
        from trader import universe
        from trader.journal import Journal
        if cfg.auto_count <= 0:
            sys.exit("config.toml 의 [universe] auto_count 가 0 이라 자동 선정이 꺼져 있습니다.")
        print(f"🔎 검색 중… (시총 상위 최대 {cfg.auto_scan_pool}개 시세 조회)")
        r = universe.scan(cfg, broker, Journal(cfg.db_path),
                          progress=lambda i, n: print(f"  {i}/{n}", end="\r", flush=True))
        print(f"1차 통과 {r['prefilter_pass']}종목 → 시세 조회 {r['scanned']}종목 → 선정 {len(r['items'])}종목 ({r['seconds']}초)")
        for i, it in enumerate(r["items"], 1):
            print(f"  {i:2d}. {it['name']}({it['code']}) {it['price']:,}원 · 전일 거래대금 {it['prev_value_eok']:,}억"
                  f" · 시총 {it['cap_eok']:,}억{' · ETF' if it['etf'] else ''}")
        return

    if args.cmd == "check":
        print(f"시장: {cfg.market.upper()} | 증권사: {broker_name(cfg)} | 모드: {cfg.mode} | 서버: {broker.env}"
              f" | 계좌: {broker.account}")
        bal = broker.balance()
        oc = bal.orderable_cash
        print(f"주문가능금액: {'(원화 증거금 — 주문 시 확인)' if oc == float('inf') else cfg.money(oc)}"
              f" | 보유 {len(bal.holdings)}종목 | 봇 예산 {cfg.money(cfg.total_krw)}")
        for h in bal.holdings.values():
            print(f"  {h.name}({h.code}) {h.qty}주 평단 {h.avg_price:,} 현재 {h.now_price:,} ({h.pnl_pct:+.2f}%)")
        try:
            if not cfg.symbols:
                raise BrokerError("분석 대상 종목이 비어 있습니다")
            q = broker.quote(cfg.symbols[0])
            print(f"시세 확인: {q.name} {cfg.money(q.price)} ({q.change_pct:+.2f}%) 장상태={q.market_status or '장외'}")
            bars = broker.daily(cfg.symbols[0], 5)
            print(f"일봉 확인: 최근 {len(bars)}일 (마지막 {bars[-1]['date'] if bars else '-'})")
        except Exception as e:  # noqa: BLE001
            print(f"❌ 시세 조회 실패: {e}")
        from trader.analyst import PROVIDERS, available
        print(f"AI 선택: {cfg.ai_label}")
        for p, info in PROVIDERS.items():
            print(f"  {info['label']:8s} {cfg.ai_models[p]:22s} {info['env']} {'설정됨' if available(p) else '❌ 없음'}")
        print("→ AI 키·모델명이 맞는지는 `python run.py aitest` 로 확인하세요.")
        return


if __name__ == "__main__":
    main()
