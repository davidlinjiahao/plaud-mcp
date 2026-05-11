from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    plaud_token: str
    plaud_device_id: str
    plaud_base_url: str = "https://api.plaud.ai"
    plaud_app_version: str = "5.3.9"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
