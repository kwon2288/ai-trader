"""설정 로딩 — config.toml(전략) + 환경변수/.env(자격증명·모드)."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """의존성 없이 .env 읽기 (이미 설정된 환경변수는 덮어쓰지 않음)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    total_krw: int
    max_per_position_krw: int
    max_positions: int
    stop_loss_pct: float
    take_profit_pct: float
    max_drawdown_pct: float
    max_orders_per_day: int
    min_confidence: float
    min_hold_days: int
    order_type: str
    ai_provider: str                 # claude | gemini | openai | consensus
    ai_models: dict[str, str]        # provider → 모델명
    ai_max_tokens: int
    symbols: list[str]                 # 핵심 종목 (항상 분석)
    auto_count: int = 10               # 자동 선정 종목 수 (0 = 끔)
    auto_min_price: int = 1000
    auto_min_cap_eok: int = 3000       # 최소 시가총액(억원)
    auto_include_etf: bool = False
    auto_scan_pool: int = 200          # 시총 상위 몇 개를 시세 조회할지
    ai_hybrid: bool = False            # 규칙으로 먼저 거르고 신호 난 종목만 AI 확인 (토큰 절약)
    market: str = "kr"                 # kr | us — 시장마다 예산·장부·손실한도가 따로
    currency: str = "KRW"
    us: dict = field(default_factory=dict)   # [us] 섹션 원본
    # 실행 모드: "dry"(주문 안 냄) | "mock"(모의투자 주문) | "live"(실계좌 주문)
    mode: str = "dry"
    mode_source: str = "env"          # env=.env 의 TRADING_MODE, web=웹에서 바꿈(data/mode.json)
    live_ack: bool = False            # 웹에서 실계좌 전환 확인을 거쳤는지
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    @property
    def ai_label(self) -> str:
        if self.ai_provider == "rules":
            return "규칙만 (AI 없음)"
        if self.ai_provider == "consensus":
            return "다수결(키가 있는 AI 전부)" + (" + 규칙 필터" if self.ai_hybrid else "")
        return (f"{self.ai_provider} ({self.ai_models.get(self.ai_provider, '?')})"
                + (" + 규칙 필터" if self.ai_hybrid else ""))

    @property
    def ledger(self) -> str:
        """일지에서 장부를 구분하는 키. 국내는 예전 그대로 mode, 미국은 mode_us."""
        return self.mode if self.market == "kr" else f"{self.mode}_{self.market}"

    @property
    def us_enabled(self) -> bool:
        return bool(self.us.get("enabled", False))

    def for_market(self, market: str) -> "Config":
        """시장별 설정. 위험관리 비율(손절·익절·확신도 등)은 공유하고 예산·종목만 바꾼다."""
        if market == "kr":
            return self
        if market != "us":
            raise ValueError(f"알 수 없는 시장: {market} (kr/us)")
        u = self.us
        return replace(
            self, market="us", currency="USD",
            total_krw=float(u.get("total_usd", 300)),               # 이름은 _krw 지만 미국에선 달러
            max_per_position_krw=float(u.get("max_per_position_usd", 150)),
            max_positions=int(u.get("max_positions", self.max_positions)),
            max_orders_per_day=int(u.get("max_orders_per_day", self.max_orders_per_day)),
            order_type=u.get("order_type", "limit"),
            symbols=[norm_symbol(x, "us") for x in u.get("symbols", [])],
            auto_count=0,                                            # 미국 자동 선정은 아직 미지원
        )

    def money(self, v) -> str:
        return f"${v:,.2f}" if self.currency == "USD" else f"{v:,.0f}원"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "STOP"


def norm_symbol(code, market: str = "kr") -> str:
    """국내: 6자리(숫자는 0 채움), 미국: 대문자 티커(BRK.B 처럼 . - 허용)."""
    c = str(code).strip().upper()
    if market == "kr" and c.isdigit():
        return c.zfill(6)
    return re.sub(r"[^A-Z0-9.\-]", "", c)


CONFIG_PATH = ROOT / "config.toml"
EXAMPLE_PATH = ROOT / "config.example.toml"


def ensure_config() -> Path:
    """config.toml 은 서버마다 다른 설정(웹에서 바꾼 값)이라 git 에 넣지 않는다.
    없으면 config.example.toml 을 복사해 만든다."""
    if not CONFIG_PATH.exists():
        import shutil
        shutil.copy(EXAMPLE_PATH, CONFIG_PATH)
    return CONFIG_PATH


def load(config_path: Path | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    raw = tomllib.loads((config_path or ensure_config()).read_text(encoding="utf-8"))
    b, r, o, a = raw["budget"], raw["risk"], raw.get("order", {}), raw.get("ai", {})
    mode = os.environ.get("TRADING_MODE", "dry").strip().lower()
    if mode not in {"dry", "mock", "live"}:
        raise ValueError(f"TRADING_MODE 는 dry/mock/live 중 하나여야 합니다 (현재: {mode})")
    data_dir = Path(os.environ["TRADER_DATA_DIR"]) if os.environ.get("TRADER_DATA_DIR") else ROOT / "data"
    from . import modefile
    override = modefile.read(data_dir)          # 웹에서 바꾼 모드가 있으면 그게 우선
    mode_source, live_ack = "env", False
    if override:
        mode, mode_source, live_ack = override["mode"], "web", bool(override.get("live_ack"))
    cfg = Config(
        total_krw=int(b["total_krw"]),
        max_per_position_krw=int(b["max_per_position_krw"]),
        max_positions=int(b["max_positions"]),
        stop_loss_pct=float(r["stop_loss_pct"]),
        take_profit_pct=float(r["take_profit_pct"]),
        max_drawdown_pct=float(r["max_drawdown_pct"]),
        max_orders_per_day=int(r["max_orders_per_day"]),
        min_confidence=float(r["min_confidence"]),
        min_hold_days=int(r.get("min_hold_days", 0)),
        order_type=o.get("type", "market"),
        ai_provider=(os.environ.get("AI_PROVIDER") or a.get("provider", "claude")).strip().lower(),
        ai_models={
            # 예전 설정의 `model` 키는 Claude 모델로 취급
            "claude": a.get("claude_model") or a.get("model") or "claude-sonnet-5-5",
            "gemini": a.get("gemini_model") or "gemini-3.8-flash",
            "openai": a.get("openai_model") or "gpt-6.1-sol",
        },
        ai_max_tokens=int(a.get("max_tokens", 4000)),
        ai_hybrid=bool(a.get("hybrid", True)),
        symbols=[norm_symbol(s, "kr") for s in raw["universe"]["symbols"]],
        auto_count=int(raw["universe"].get("auto_count", 10)),
        auto_min_price=int(raw["universe"].get("auto_min_price", 1000)),
        auto_min_cap_eok=int(raw["universe"].get("auto_min_cap_eok", 3000)),
        auto_include_etf=bool(raw["universe"].get("auto_include_etf", False)),
        auto_scan_pool=int(raw["universe"].get("auto_scan_pool", 200)),
        mode=mode, mode_source=mode_source, live_ack=live_ack,
        us=dict(raw.get("us", {})),
        data_dir=data_dir,
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
