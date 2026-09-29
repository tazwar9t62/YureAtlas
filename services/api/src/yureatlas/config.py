from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://yureatlas:yureatlas_local@127.0.0.1:55432/yureatlas"
    artifact_dir: Path = Path("data/artifacts")
    allowed_origins: str = "http://127.0.0.1:3000,http://localhost:3000"
    history_start: str = "2015-01-01"
    schedule_enabled: bool = True


settings = Settings()
