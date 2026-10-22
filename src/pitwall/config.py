from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PITWALL_", env_file=".env", extra="ignore")

    llm_base_url: AnyHttpUrl = "http://localhost:20128/v1"
    llm_api_key: SecretStr = SecretStr("")
    intent_model: str = ""
    insight_model: str = ""
    embedding_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
