"""Runtime configuration, read from environment variables (see .env.example).

Nothing secret is hardcoded. All paths are relative to PROJECT_ROOT unless overridden.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (avoids a python-dotenv dependency). Never overrides real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    db_path: Path
    raw_dir: Path
    export_dir: Path
    eval_dir: Path
    log_dir: Path

    greenhouse_tokens: list[str]
    greenhouse_fallback_tokens: list[str]
    lever_sites: list[str]
    lever_fallback_sites: list[str]
    hn_thread_id: str | None  # optional manual override if thread discovery is blocked

    user_agent: str
    min_interval_s: float  # minimum seconds between requests to one domain (1 rps => 1.0)
    timeout_s: float
    max_retries: int

    llm_provider: str  # anthropic | openai | mock
    llm_model: str
    llm_api_key: str | None
    llm_price_in_per_mtok: float
    llm_price_out_per_mtok: float
    llm_max_input_chars: int
    llm_budget_usd: float  # hard stop for a single run

    fixtures_dir: Path | None = None  # replay recorded/synthetic responses instead of the network
    extra: dict = field(default_factory=dict)


def load_settings(**overrides) -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    provider = os.environ.get("LLM_PROVIDER", "mock").lower()
    key_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    default_model = {"anthropic": "claude-haiku-4-5", "openai": "gpt-4o-mini"}.get(provider, "mock-heuristic-v1")
    data = os.environ.get("DATA_DIR", str(PROJECT_ROOT / "data"))
    s = Settings(
        db_path=Path(os.environ.get("DB_PATH", f"{data}/jobs.db")),
        raw_dir=Path(f"{data}/raw"),
        export_dir=Path(f"{data}/exports"),
        eval_dir=Path(f"{data}/evaluation"),
        log_dir=Path(os.environ.get("LOG_DIR", str(PROJECT_ROOT / "logs"))),
        greenhouse_tokens=_list("GREENHOUSE_TOKENS", "stripe,airbnb,databricks,figma"),
        greenhouse_fallback_tokens=_list("GREENHOUSE_FALLBACK_TOKENS", "dropbox,datadog,robinhood"),
        lever_sites=_list("LEVER_SITES", "palantir,plaid"),
        lever_fallback_sites=_list("LEVER_FALLBACK_SITES", "spotify,netflix"),
        hn_thread_id=os.environ.get("HN_THREAD_ID") or None,
        user_agent=os.environ.get(
            "USER_AGENT", "job-intel-takehome/1.0 (+contact: set USER_AGENT with your email)"
        ),
        min_interval_s=float(os.environ.get("MIN_INTERVAL_S", "1.0")),
        timeout_s=float(os.environ.get("HTTP_TIMEOUT_S", "20")),
        max_retries=int(os.environ.get("HTTP_MAX_RETRIES", "4")),
        llm_provider=provider,
        llm_model=os.environ.get("LLM_MODEL", default_model),
        llm_api_key=os.environ.get(key_var) if key_var else None,
        # USD per million tokens; set to your provider's current list price.
        llm_price_in_per_mtok=float(os.environ.get("LLM_PRICE_IN_PER_MTOK", "1.0")),
        llm_price_out_per_mtok=float(os.environ.get("LLM_PRICE_OUT_PER_MTOK", "5.0")),
        llm_max_input_chars=int(os.environ.get("LLM_MAX_INPUT_CHARS", "6000")),
        llm_budget_usd=float(os.environ.get("LLM_BUDGET_USD", "5.0")),
    )
    fx = os.environ.get("FIXTURES_DIR")
    if fx:
        s.fixtures_dir = Path(fx)
    for k, v in overrides.items():
        setattr(s, k, v)
    for d in (s.raw_dir, s.export_dir, s.eval_dir, s.log_dir, s.db_path.parent):
        Path(d).mkdir(parents=True, exist_ok=True)
    return s
