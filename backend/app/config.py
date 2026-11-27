from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    app_env: str = "development"
    database_path: str = "data/tracemuse.sqlite3"
    max_upload_mb: int = Field(default=100, ge=1, le=200)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-flash"
    legal_snapshot_path: str = ""

    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("deepseek_model")
    @classmethod
    def normalize_deepseek_model(cls, model: str) -> str:
        model = model.strip()
        # DeepSeek accepts these retired aliases for completions but lists the canonical ID.
        if model in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
            return "deepseek-flash"
        return model


settings = Settings()
