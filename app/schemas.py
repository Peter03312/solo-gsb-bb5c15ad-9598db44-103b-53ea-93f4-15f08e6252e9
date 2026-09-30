from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class Interval(BaseModel):
    """左闭右开 [start, end)，按 Unicode 码点计。"""

    start: int = Field(ge=0)
    end: int = Field(ge=1)


class RecordCreateRequest(BaseModel):
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class RecordReviseRequest(BaseModel):
    base_version: int = Field(ge=1)
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class PublishRequest(BaseModel):
    version: int = Field(ge=1)
    # 两种到期方式二选一，均不填则令牌永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _only_one_expiry(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class RecordResponse(BaseModel):
    record_id: UUID
    version: int


class SnapshotInfo(BaseModel):
    snapshot_id: UUID
    version: int
    status: str
    published_at: datetime


class RecordStatusResponse(BaseModel):
    record_id: UUID
    current_version: int
    snapshots: list[SnapshotInfo]


class PublishResponse(BaseModel):
    snapshot_id: UUID
    record_id: UUID
    version: int
    status: str
    token: str
    expires_at: datetime | None


class RevokeResponse(BaseModel):
    token: str
    revoked: bool
    already_revoked: bool


class PublicSnapshotResponse(BaseModel):
    record_id: UUID
    version: int
    status: str
    redacted_text: str
