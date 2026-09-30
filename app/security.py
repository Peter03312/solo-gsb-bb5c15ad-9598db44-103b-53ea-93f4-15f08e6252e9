import hmac

from fastapi import Header, HTTPException, status

from .config import settings


def require_service_key(x_service_key: str | None = Header(default=None)) -> None:
    """维护接口鉴权：缺少或服务密钥不符时整次拒绝。"""
    if x_service_key is None or not hmac.compare_digest(x_service_key, settings.service_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid service key",
        )
