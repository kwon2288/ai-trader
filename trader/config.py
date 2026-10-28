"""설정 로딩 — config.toml(전략) + 환경변수/.env(자격증명·모드)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
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
    # 실행 모드: "dry"(주문 안 냄) | "mock"(모의투자 주문) | "live"(실계좌 주문)
    mode: str = "dry"
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    @property
    def ai_label(self) -> str:
        if self.ai_provider == "consensus":
            return "다수결(키가 있는 AI 전부)"
        return f"{self.ai_provider} ({self.ai_models.get(self.ai_provider, '?')})"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "STOP"


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
        symbols=[str(s).zfill(6) for s in raw["universe"]["symbols"]],
        auto_count=int(raw["universe"].get("auto_count", 10)),
        auto_min_price=int(raw["universe"].get("auto_min_price", 1000)),
        auto_min_cap_eok=int(raw["universe"].get("auto_min_cap_eok", 3000)),
        auto_include_etf=bool(raw["universe"].get("auto_include_etf", False)),
        auto_scan_pool=int(raw["universe"].get("auto_scan_pool", 200)),
        mode=mode,
    )
    if os.environ.get("TRADER_DATA_DIR"):
        cfg.data_dir = Path(os.environ["TRADER_DATA_DIR"])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
