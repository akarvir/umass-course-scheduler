from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://coursewatch:coursewatch@localhost:5432/coursewatch"
    app_origin: str = "http://localhost:3000"
    environment: str = "development"
    auth_secret: str = "local-development-only-change-this-secret"
    email_backend: str = "smtp"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    email_from: str = "Course Watch <alerts@localhost>"
    resend_api_key: str = ""
    poll_interval_seconds: int = 300
    spire_request_interval_seconds: float = 3

    @model_validator(mode="after")
    def validate_settings(self):
        if self.poll_interval_seconds < 120 or self.spire_request_interval_seconds < 1:
            raise ValueError("Polling must be at least 120s, with at least 1s between requests")
        if self.email_backend not in {"smtp", "resend"}:
            raise ValueError("EMAIL_BACKEND must be smtp or resend")
        if self.environment == "production":
            if len(self.auth_secret) < 32 or self.auth_secret in {
                "local-development-only-change-this-secret",
                "replace-with-a-random-secret-at-least-32-characters",
            }:
                raise ValueError("Set a private AUTH_SECRET of at least 32 characters")
            if not self.app_origin.startswith("https://"):
                raise ValueError("Production APP_ORIGIN must use HTTPS")
            if self.email_backend == "resend" and not self.resend_api_key:
                raise ValueError("Set RESEND_API_KEY")
        return self


@lru_cache
def settings():
    return Settings()
