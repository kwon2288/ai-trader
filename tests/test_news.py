"""뉴스·공시 — RSS·DART 파싱, 캐시, 켜고 끄기, 봇 연결."""
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trader import news  # noqa: E402

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>삼성전자, 신규 공장 착공 - 한국경제</title><source>한국경제</source>
<pubDate>Thu, 29 Oct 2026 01:00:00 GMT</pubDate></item>
<item><title>반도체 업황 회복 - 매일경제</title><source>매일경제</source><pubDate>Wed, 28 Oct 2026 01:00:00 GMT</pubDate></item>
</channel></rss>"""
calls = []


class Resp:
    def __init__(self, content=b"", js=None):
        self.content, self._js = content, js

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


def fake_get(url, params=None, headers=None, timeout=None):
    calls.append(url)
    if "news.google.com" in url:
        return Resp(RSS.encode())
    if "corpCode" in url:
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("CORPCODE.xml", "<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code></list></result>")
        return Resp(b.getvalue())
    if "list.json" in url:
        assert params["corp_code"] == "00126380"
        return Resp(js={"status": "000", "list": [{"rcept_dt": "20261029", "report_nm": "주요사항보고서(자기주식취득결정)"}]})
    raise AssertionError(url)


news.requests.get = fake_get


def test_enrich_news_and_dart_with_cache():
    d = Path(tempfile.mkdtemp())
    os.environ["DART_API_KEY"] = "k"
    try:
        c = [{"code": "005930", "name": "삼성전자"}]
        assert news.enrich(c, "kr", d) == 1
        assert c[0]["news"][0].endswith("삼성전자, 신규 공장 착공") and "자기주식" in c[0]["disclosures"][0]
        n = len(calls)
        c2 = [{"code": "005930", "name": "삼성전자"}]
        news.enrich(c2, "kr", d)
        assert len(calls) == n and c2[0]["news"]                    # 1시간 캐시
        u = [{"code": "KO", "name": "코카콜라"}]
        news.enrich(u, "us", d)
        assert "disclosures" not in u[0] and "hl=en-US" in calls[-1]
    finally:
        os.environ.pop("DART_API_KEY")


def test_bot_attaches_news_only_when_enabled():
    import test_bot as tb
    seen = []
    orig = news.enrich
    news.enrich = lambda cands, market, data_dir, say=None: seen.append(len(cands)) or 0
    try:
        _, _, ai, bot = tb.setup(decisions=[], ai_hybrid=False)
        bot.run()
        assert seen == []
        _, _, ai, bot = tb.setup(decisions=[], ai_hybrid=False, ai_news=True)
        out = bot.run()
        assert seen and "📰" in out
    finally:
        news.enrich = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
