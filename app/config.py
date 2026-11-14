"""App configuration. Secrets come from env vars only — never hardcoded."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    gemini_api_key: str = ""
    gemini_api_key_secondary: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    auto_approve_limit_inr: int = 500_000
    langsmith_api_key: str = ""
    langsmith_project: str = "elric"
    # JWT secret for access-token signing. No default: the app refuses
    # to start without one (see app/main.py). Never commit a real value.
    jwt_secret: str = ""


settings = Settings()
