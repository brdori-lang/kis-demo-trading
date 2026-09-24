from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    KIS_APP_KEY: str
    KIS_APP_SECRET: str
    KIS_ACCOUNT_NO: str
    KIS_ENV: Literal["virtual"] = "virtual"
    DATABASE_URL: str | None = None
    PAPER_ORDER_ENABLED: bool = False
    KIS_VIRTUAL_ORDER_SUBMIT_ENABLED: bool = False
    MAX_ORDER_AMOUNT: int = 1_000_000
    MAX_ORDER_QUANTITY: int = 100
    AURA_INTEGRATION_READ_KEY: str = ""
    KIS_HTS_ID: str = ""  # H0STCNI9 realtime order notices (tr_key); empty disables them

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )


settings = Settings()
