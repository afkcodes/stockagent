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

    @property
    def max_allocation_inr(self) -> float:
        return self.capital_inr * self.max_allocation_pct

    @property
    def max_risk_per_trade_inr(self) -> float:
        return self.capital_inr * self.max_risk_per_trade_pct


settings = Settings()
