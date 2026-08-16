# -*- coding: utf-8 -*-
"""Schemas for the local Codex App Server account control surface."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CodexAccountStatusSchema(BaseModel):
    status: str
    auth_method: Optional[str] = None
    email: Optional[str] = None
    plan_type: Optional[str] = None
    requires_openai_auth: Optional[bool] = None


class CodexRateLimitWindowSchema(BaseModel):
    bucket: Literal["primary", "secondary"]
    used_percent: int = Field(..., ge=0, le=100)
    window_duration_minutes: Optional[int] = Field(None, ge=0)
    resets_at: Optional[int] = Field(None, ge=0)


class CodexRateLimitSnapshotSchema(BaseModel):
    limit_id: Optional[str] = None
    limit_name: Optional[str] = None
    plan_type: Optional[str] = None
    primary: Optional[CodexRateLimitWindowSchema] = None
    secondary: Optional[CodexRateLimitWindowSchema] = None
    spend_control_reached: Optional[bool] = None
    rate_limit_reached_type: Optional[str] = None


class CodexRateLimitsSchema(BaseModel):
    snapshots: List[CodexRateLimitSnapshotSchema] = Field(default_factory=list)
    available_reset_credits: Optional[int] = Field(None, ge=0)


class CodexAccountNotificationSchema(BaseModel):
    method: str
    status: str
    account: Optional[CodexAccountStatusSchema] = None
    rate_limits: Optional[CodexRateLimitsSchema] = None
    login_id: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None


class CodexAccountStatusResponse(BaseModel):
    account: CodexAccountStatusSchema
    rate_limits: Optional[CodexRateLimitsSchema] = None
    rate_limit_error_code: Optional[str] = None
    notifications: List[CodexAccountNotificationSchema] = Field(default_factory=list)


class CodexAccountLoginRequest(BaseModel):
    mode: Literal["browser", "device_code"] = "browser"


class CodexAccountLoginResponse(BaseModel):
    mode: Literal["browser", "device_code"]
    status: Literal["pending"]
    login_id: str
    auth_url: Optional[str] = None
    verification_url: Optional[str] = None
    user_code: Optional[str] = None


class CodexAccountCancelRequest(BaseModel):
    login_id: str = Field(..., min_length=1, max_length=256)


class CodexAccountActionResponse(BaseModel):
    status: str


__all__ = [
    "CodexAccountActionResponse",
    "CodexAccountCancelRequest",
    "CodexAccountLoginRequest",
    "CodexAccountLoginResponse",
    "CodexAccountNotificationSchema",
    "CodexAccountStatusResponse",
    "CodexAccountStatusSchema",
    "CodexRateLimitSnapshotSchema",
    "CodexRateLimitWindowSchema",
    "CodexRateLimitsSchema",
]
