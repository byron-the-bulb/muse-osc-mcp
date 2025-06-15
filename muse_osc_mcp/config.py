"""Application configuration.
Reads database connection settings from environment variables.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
import os
from typing import ClassVar
from pydantic_settings import SettingsConfigDict

class Settings(BaseSettings):
    db_host: str = Field(default="10.0.33.78", validation_alias="DATABASE_HOST")
    db_port: int = Field(default=5432, validation_alias="DATABASE_PORT")
    db_name: str = Field(default="muse", validation_alias="DATABASE_NAME")
    db_user: str = Field(default="postgres", validation_alias="DATABASE_USERNAME")
    db_password: str = Field(default="postgres", validation_alias="DATABASE_PASSWORD")

    osc_port: int = Field(default=5000, validation_alias="OSC_PORT")
    session_user: str = Field(default="unknown", validation_alias="SESSION_USER")
    db_pool_warmup_connections: int = Field(default=15, validation_alias="DB_POOL_WARMUP_CONNECTIONS") # Match default engine pool_size
    batch_interval_seconds: float = Field(default=1.0, validation_alias="BATCH_INTERVAL_SECONDS")
    batch_max_size: int = Field(default=1000, validation_alias="BATCH_MAX_SIZE") # Max items per batch type before forcing a commit (not yet implemented in commit logic)

    # Load variables from the process environment first, then from .env file.
    # Determine the .env file to load based on APP_ENV_FILE environment variable
    # Defaults to '.env' if APP_ENV_FILE is not set.
    app_env_file: ClassVar[str] = os.getenv("APP_ENV_FILE", ".env")
    model_config = SettingsConfigDict(env_file=app_env_file, env_file_encoding="utf-8", extra="ignore")

    @property
    def async_db_url(self) -> str:
        """Return SQLAlchemy async URL for asyncpg driver."""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"  # noqa: S603,S607
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

settings = Settings()
