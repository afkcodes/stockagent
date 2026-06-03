from pathlib import Path

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    capital_inr: float = Field(default=100_000.0, description="Paper trading capital")
    max_allocation_pct: float = Field(default=0.20, description="Max % of capital per stock")
    max_risk_per_trade_pct: float = Field(default=0.05, description="Max loss per trade as % of capital")

    stockagent_db_path: Path = Path("data/stockagent.db")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    model_technical: str = "deepseek/deepseek-chat"
    model_fundamental: str = "google/gemini-2.5-flash"
    model_sentiment: str = "google/gemini-2.5-flash"
    model_macro: str = "google/gemini-2.5-flash"
    model_coordinator: str = "minimax/minimax-m2"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Auto-learning (see docs/autolearn_design.md) ---
    # Master kill-switch. While False, learned adjustments are computed + logged
    # (shadow) but never applied to live conviction/sizing. Phase 3+ honours this.
    autolearn_active: bool = Field(default=False, description="Apply learned adjustments to live picks")
    # A pattern/reliability bucket needs at least this many closed trades before
    # it can be marked is_active=1 and influence anything.
    autolearn_min_n: int = Field(default=8, description="Min closed trades for a learned bucket to activate")
    # Rolling window for mining; only trades closed within this many days are
    # pooled. None/0 = use the whole corpus.
    autolearn_window_days: int = Field(default=365, description="Rolling window (days) for mining; 0 = all history")

    @property
    def max_allocation_inr(self) -> float:
        return self.capital_inr * self.max_allocation_pct

    @property
    def max_risk_per_trade_inr(self) -> float:
        return self.capital_inr * self.max_risk_per_trade_pct


settings = Settings()
