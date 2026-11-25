"""项目二全局配置管理（自动复用系统环境变量与项目一的 Langfuse 本地凭证）。"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用运行、数据库、LLM 与 SQL 护栏配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vm_host: str = "192.168.150.130"
    postgres_dsn: str = "postgresql://rag_user:rag_password_change_me@192.168.150.130:5432/ecom_rag"
    bi_schema: str = "ecom_bi"

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    langfuse_enabled: bool = True
    langfuse_host: str = "http://192.168.150.130:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    sql_statement_timeout_ms: int = 3000
    sql_max_rows: int = 200
    sql_max_retries: int = 2

    @model_validator(mode="after")
    def _resolve_keys(self) -> "Settings":
        if not self.llm_api_key or "please-replace" in self.llm_api_key:
            self.llm_api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""

        # 若项目二 .env 未重复填 Langfuse Key，自动从同级项目一 .env 读取，免重复配置
        if not self.langfuse_public_key or not self.langfuse_secret_key:
            sibling_env = Path(__file__).resolve().parents[3] / "ecom-agentic-rag" / ".env"
            if sibling_env.is_file():
                vals = dotenv_values(sibling_env)
                if not self.langfuse_public_key and vals.get("LANGFUSE_PUBLIC_KEY"):
                    self.langfuse_public_key = str(vals["LANGFUSE_PUBLIC_KEY"]).strip()
                if not self.langfuse_secret_key and vals.get("LANGFUSE_SECRET_KEY"):
                    self.langfuse_secret_key = str(vals["LANGFUSE_SECRET_KEY"]).strip()
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
