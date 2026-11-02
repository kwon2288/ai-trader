"""웹 화면 — 백테스트 · 전략."""

from flask import flash, redirect, render_template, request, url_for

from trader import config as cfgmod  # noqa: E402
from trader.journal import Journal  # noqa: E402
from web import settings_io  # noqa: E402
from web import app as webapp  # noqa: E402,F401
from web.app import app, login_required  # noqa: E402,F401


# ── 백테스트 ───────────────────────────────────────────────
from trader.strategy import BENCH  # noqa: E402


def _run_backtest(market: str, p: dict) -> None:
    from trader import backtest
    from trader.runner import make_broker, market_cfg
    c = market_cfg(webapp.cfg(), market)
    j = Journal(c.db_path)
    prog = lambda k, n, code: webapp._job.update(progress=f"{k}/{n} {code}")  # noqa: E731
    try:
        if p.get("sweep"):
            from trader.signals import label
            r = backtest.sweep(c, make_broker(c), p["days"], p["symbols"], BENCH[market][0], progress=prog,
                               kind=p.get("kind", "full"))
            best = r["rows"][0] if r["rows"] else None
            j.add_log(c.ledger, "backtest", "web", f"{'단기 매매' if r['kind'] == 'short' else '자동'} 비교 "
                      f"{len(r['rows'])}개 조합 ({r['seconds']}초)" + (
                f" — 추천: {label(best['signal'])} · 손절 {best['stop']}% · 익절 +{best['take']}% · "
                f"보유 {best['min_hold']}~{best['max_hold'] or '∞'}일 · "
                f"시장필터 {'켬' if best['market_filter'] else '끔'} · 코어 {best['core_pct']}% → 수익률 "
                f"{best['return_pct']:+.2f}% (낙폭 {best['mdd_pct']}%)" if best else ""))
            return
        res = backtest.run_job(c, make_broker(c), p["days"], p["stop"], p["take"], p["symbols"], BENCH[market][0],
                               progress=prog, min_hold=p.get("min_hold"), market_filter=p.get("market_filter"),
                               core_pct=p.get("core_pct"), signal_mode=p.get("signal"), max_hold=p.get("max_hold"))
        j.add_log(c.ledger, "backtest", "web",
                  f"백테스트 {res.get('start')}~{res.get('end')}: 수익률 {res['return_pct']:+.2f}% "
                  f"(기준 {res.get('bench_pct')}%), 최대 낙폭 {res['mdd_pct']}%, 거래 {res['n']}회 ({res['seconds']}초)")
    except Exception as e:  # noqa: BLE001
        j.add_log(c.ledger, "backtest", "web", f"❌ 백테스트 실패: {e}")


def lines_svg(series: list[list[float]], labels: list[str], classes: list[str]) -> str:
    pts_all = [v for s in series for v in s if v is not None]
    if not pts_all or max(len(s) for s in series) < 2:
        return ""
    w, h, pad = 600, 170, 6
    lo, hi = min(pts_all), max(pts_all)
    span = (hi - lo) or 1
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="자산 비교">']
    for s, cl in zip(series, classes):
        n = len(s)
        pts = " ".join(f"{pad + i * (w - 2 * pad) / (n - 1):.1f},{h - pad - (v - lo) * (h - 2 * pad) / span:.1f}"
                       for i, v in enumerate(s) if v is not None)
        out.append(f'<polyline points="{pts}" class="line {cl}"/>')
    out.append("</svg>")
    return "".join(out)


@app.route("/backtest", methods=["GET", "POST"])
@login_required
def backtest_page():
    from trader import backtest
    m = webapp.current_market()
    base = webapp.cfg()
    c = base.for_market(m)
    if request.method == "POST":
        f = request.form
        try:
            days = int(f.get("days", "500"))
            stop = float(f.get("stop", c.stop_loss_pct))
            take = float(f.get("take", c.take_profit_pct))
            min_hold = int(f.get("min_hold", c.min_hold_days))
            core_pct = int(float(f.get("core_pct", 0) or 0))
            max_hold = int(float(f.get("max_hold", 0) or 0))
        except ValueError:
            flash("숫자를 확인하세요.", "err")
            return redirect(url_for("backtest_page"))
        from trader.signals import MODES
        signal = f.get("signal", "trend") if f.get("signal") in MODES else "trend"
        if days not in (250, 500, 750) or not (-50 <= stop < 0) or not (0 < take <= 200) \
                or not (0 <= min_hold <= 60) or not (0 <= core_pct <= 90) or not (0 <= max_hold <= 60):
            flash("기간·손절(-50~0)·익절(0~200)·보유일(0~60)·코어(0~90%)를 확인하세요.", "err")
            return redirect(url_for("backtest_page"))
        syms = [cfgmod.norm_symbol(x, m) for x in (f.get("symbols") or "").replace(",", " ").split()]
        syms = list(dict.fromkeys(x for x in syms if x))[:40] or c.symbols[:40]
        kind = {"sweep": "full", "short": "short"}.get(f.get("mode") or "")
        sweep = bool(kind)
        err = webapp._start_job("backtest", m, {"days": days, "stop": stop, "take": take, "symbols": syms, "sweep": sweep,
                                         "kind": kind or "full", "min_hold": min_hold, "signal": signal,
                                         "max_hold": max_hold, "market_filter": bool(f.get("market_filter")),
                                         "core_pct": core_pct})
        n = backtest.grid_size(backtest.GRIDS[kind]) if kind else 0
        flash(err or (f"🔍 {'단기 매매' if kind == 'short' else '자동'} 비교를 시작했어요 — {len(syms)}종목 × 조합 {n}개, "
                      f"보통 1~3분 (데이터를 처음 받으면 더)." if sweep else
                      f"🧪 백테스트를 시작했어요 — {len(syms)}종목, 처음엔 데이터 받느라 1~3분 걸려요."),
              "err" if err else "ok")
        return redirect(url_for("backtest_page") + ("#sweep" if sweep else ""))
    r = backtest.load(c.data_dir, m)
    chart = ""
    if r and r.get("curve"):
        eq = [x["equity"] for x in r["curve"]]
        bmap = {x["date"]: x["v"] for x in r.get("bench_curve") or []}
        chart = lines_svg([eq, [bmap.get(x["date"]) for x in r["curve"]]] if bmap else [eq],
                          ["봇 전략", "기준"], ["up" if eq[-1] >= eq[0] else "down", "bench"])
    from trader import strategy
    from trader.signals import MODES
    return render_template("backtest.html", c=c, base=base, market=m, r=r, chart=chart, bench=BENCH[m],
                           busy=webapp._busy(c), job=webapp._job, sw=backtest.load_sweep(c.data_dir, m), sg=strategy.settings(c),
                           modes={k: v[0] for k, v in MODES.items()},
                           n_full=backtest.grid_size(backtest.GRID), n_short=backtest.grid_size(backtest.SHORT_GRID))


@app.post("/strategy/save")
@login_required
def strategy_save():
    f = request.form
    try:
        core = int(float(f.get("core_pct", "0") or 0))
    except ValueError:
        core = -1
    try:
        max_hold = int(float(f.get("max_hold_days", "0") or 0))
    except ValueError:
        max_hold = -1
    from trader.signals import MODES
    if not 0 <= core <= 90 or not 0 <= max_hold <= 60 or f.get("signal", "trend") not in MODES:
        flash("코어 비중은 0~90%, 최대 보유일은 0~60일 입니다.", "err")
        return redirect(url_for("settings") + "#strategy")
    text = webapp.CONFIG_PATH.read_text(encoding="utf-8")
    text = settings_io._set(text, "strategy", "signal", f.get("signal", "trend"))
    text = settings_io._set(text, "strategy", "max_hold_days", max_hold)
    text = settings_io._set(text, "strategy", "market_filter", bool(f.get("market_filter")))
    text = settings_io._set(text, "strategy", "core_pct", core)
    for mk in ("kr", "us"):
        v = cfgmod.norm_symbol((f.get(f"core_{mk}") or "").strip(), mk)
        if v:
            text = settings_io._set(text, "strategy", f"core_{mk}", v)
    webapp.CONFIG_PATH.write_text(text, encoding="utf-8")
    flash("전략 설정을 저장했습니다. 다음 실행부터 적용돼요.", "ok")
    return redirect(url_for("settings") + "#strategy")


@app.post("/strategy/apply")
@login_required
def strategy_apply():
    """자동 비교 결과 한 줄(또는 설정 화면)을 실제 설정에 적용."""
    f = request.form
    try:
        stop, take = float(f["stop"]), float(f["take"])
        hold, core = int(f["min_hold"]), int(float(f["core_pct"]))
        max_hold = int(float(f.get("max_hold", 0) or 0))
    except (KeyError, ValueError):
        flash("값이 올바르지 않습니다.", "err")
        return redirect(request.referrer or url_for("settings"))
    from trader.signals import MODES, label
    signal = f.get("signal") or "trend"
    if not (-50 <= stop < 0 and 0 < take <= 200 and 0 <= hold <= 60 and 0 <= core <= 90
            and 0 <= max_hold <= 60 and signal in MODES):
        flash("범위를 벗어난 값입니다.", "err")
        return redirect(request.referrer or url_for("settings"))
    text = webapp.CONFIG_PATH.read_text(encoding="utf-8")
    text = settings_io._set(text, "risk", "stop_loss_pct", stop)
    text = settings_io._set(text, "risk", "take_profit_pct", take)
    text = settings_io._set(text, "risk", "min_hold_days", hold)
    text = settings_io._set(text, "strategy", "market_filter", f.get("market_filter") in ("1", "on", "true", "True"))
    text = settings_io._set(text, "strategy", "core_pct", core)
    text = settings_io._set(text, "strategy", "signal", signal)
    text = settings_io._set(text, "strategy", "max_hold_days", max_hold)
    for mk in ("kr", "us"):
        v = (f.get(f"core_{mk}") or "").strip()
        if v:
            v = cfgmod.norm_symbol(v, mk)
            if v:
                text = settings_io._set(text, "strategy", f"core_{mk}", v)
    webapp.CONFIG_PATH.write_text(text, encoding="utf-8")
    c = webapp.cfg()
    Journal(c.db_path).add_log(c.ledger, "strategy", "web",
                               f"전략 적용: {label(signal)} · 최대 보유 {max_hold or '제한 없음'}{'일' if max_hold else ''} · 손절 {stop}% · 익절 +{take}% · 최소 보유 {hold}일 · 시장필터 "
                               f"{'켬' if f.get('market_filter') in ('1', 'on', 'true', 'True') else '끔'} · 코어 {core}%")
    flash(f"적용했어요: {label(signal)} · 최대 보유 {str(max_hold) + '일' if max_hold else '제한 없음'} · 손절 {stop}% · 익절 +{take}% · 최소 보유 {hold}일 · 시장 필터 "
          f"{'켬' if f.get('market_filter') in ('1', 'on', 'true', 'True') else '끔'} · 코어 {core}% — 다음 실행부터 (국내·미국 공통)", "ok")
    return redirect(request.referrer or url_for("settings"))
