"""Cloud model configuration. Secrets are never written to execution reports."""
import os
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field, SecretStr, field_validator
from .governance import Contract


class Settings(Contract):
    model: str = Field(min_length=1)
    base_url: str
    api_key: SecretStr
    sre_bench_path: Path
    request_timeout: float = Field(default=60, gt=0, le=300)
    max_tokens: int = Field(default=4096, ge=64, le=16384)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an HTTP(S) API base URL without credentials or query")
        return value

    @classmethod
    def from_env(cls):
        keys = {"model": "LLM_MODEL", "base_url": "LLM_BASE_URL",
                "api_key": "LLM_API_KEY", "sre_bench_path": "SRE_BENCH_PATH"}
        missing = [v for v in keys.values() if not os.environ.get(v)]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        return cls(**{k: os.environ[v] for k, v in keys.items()})

    def build_model(self):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=self.model, base_url=self.base_url,
                          api_key=self.api_key.get_secret_value(),
                          timeout=self.request_timeout, max_retries=2,
                          max_tokens=self.max_tokens,
                          use_responses_api=False)
