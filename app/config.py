"""服务配置：全部通过环境变量注入（可搭配 .env 文件）。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL 连接串，Compose 中指向 db 服务
    database_url: str = "postgresql+psycopg2://redact:redact@localhost:5432/redact"
    # 维护人员服务密钥，必填，未配置时服务拒绝启动
    service_key: str
    # 启动时等待数据库就绪的重试参数
    db_connect_retries: int = 30
    db_connect_retry_delay_seconds: float = 1.0


settings = Settings()
