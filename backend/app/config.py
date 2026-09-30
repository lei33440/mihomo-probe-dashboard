"""应用配置，全部通过环境变量加载。"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mihomo
    mihomo_api_url: str = "http://mihomo:9090"
    mihomo_secret: str = ""

    # Probe
    probe_interval: int = 60
    probe_timeout: int = 5000
    probe_url: str = "https://www.gstatic.com/generate_204"
    probe_concurrency: int = 20
    probe_failure_threshold: int = 3

    # Aggregation
    agg_5min_enabled: bool = True
    agg_1h_enabled: bool = True
    retention_raw_days: int = 7
    retention_5min_days: int = 30

    # Auth
    admin_username: str = "admin"
    admin_password: str = "changeme"
    jwt_secret: str = "please-change-me-to-a-random-32-byte-string"
    jwt_expires_hours: int = 24
    cors_origins: str = ""                # 逗号分隔，留空=不限制来源（生产请填 https://your.domain.com）

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jwt_secret_valid(self) -> bool:
        if not self.jwt_secret:
            return False
        if self.jwt_secret == "please-change-me-to-a-random-32-byte-string":
            return False
        return len(self.jwt_secret) >= 32

    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    tz: str = "Asia/Shanghai"

    # ===== Demo 模式（本地无 mihomo 时开启，会注入假节点和模拟延迟） =====
    demo_mode: bool = False

    @property
    def db_path(self) -> Path:
        # Docker 容器内用 /app/data，本地开发用项目根目录 data/
        import os
        if os.path.isdir("/app/data"):
            path = Path("/app/data") / "probe.db"
        else:
            path = BASE_DIR.parent / "data" / "probe.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "static"


settings = Settings()