"""语义证据数据索引路由：按页面证据反查语义预测、补交出处与清单回写。

与通用资源路由分开注册，路径固定在 /api/semantic-evidence/* 下，
必须在 /api/{module} 动态路由之前挂载。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.semantic_evidence import semantic_evidence_service

router = APIRouter(prefix="/api/semantic-evidence", tags=["语义证据数据索引"])


class IndexPayload(BaseModel):
    sentiment_record_id: str
    intent: str = ""
    nlu_record_id: str = ""
    score: str = ""


class SupplementPayload(BaseModel):
    annotation_version: str
    page_uri: str
    intent: str = ""
    text_snippet: str = ""
    sentiment_record_id: str = ""
    # False 表示只暂存（补交中断），True 才真正确认出处。
    commit: bool = True
    remark: str | None = None


@router.get("/search")
def search_evidence(
    page_uri: str = "",
    intent: str = "",
    sentiment_record_id: str = "",
    text_snippet: str = "",
    include_unconfirmed: bool = False,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """按页面 + 意图 + 情感标注/文本片段反查证据。

    带 page_uri 时只返回该页出处已确认的条目；未确认/属于其他页面的同文本记录
    会出现在 blocked_unconfirmed 里，提示运营回情感标注核对原片段。
    """
    return semantic_evidence_service.search_evidence(
        page_uri=page_uri,
        intent=intent,
        sentiment_record_id=sentiment_record_id,
        text_snippet=text_snippet,
        include_unconfirmed=include_unconfirmed,
        page=page,
        size=size,
    )


@router.post("/index")
def index_evidence(payload: IndexPayload) -> dict[str, Any]:
    """显式触发：消费一条情感标注并写入语义证据索引。"""
    return semantic_evidence_service.index_from_sentiment(
        sentiment_record_id=payload.sentiment_record_id,
        intent=payload.intent,
        nlu_record_id=payload.nlu_record_id,
        score=payload.score,
    )


@router.post("/supplement-provenance")
def supplement_provenance(payload: SupplementPayload) -> dict[str, Any]:
    """情感标注补交页面出处（支持中断暂存与重投确认）。"""
    return semantic_evidence_service.supplement_provenance(
        annotation_version=payload.annotation_version,
        page_uri=payload.page_uri,
        intent=payload.intent,
        text_snippet=payload.text_snippet,
        sentiment_record_id=payload.sentiment_record_id,
        commit=payload.commit,
    )


@router.get("/checklist")
def delivery_checklist() -> dict[str, Any]:
    """标注交付清单：仍缺页面出处、需要情感标注补交的证据条目。"""
    return semantic_evidence_service.delivery_checklist()


@router.get("/legacy/{legacy_id}")
def resolve_legacy(legacy_id: str) -> dict[str, Any]:
    """用历史预测记录里的旧编号回查当前出处记录。"""
    return semantic_evidence_service.resolve_legacy_id(legacy_id)


@router.get("/stats")
def evidence_stats() -> dict[str, Any]:
    """全站计数与逐页出处统计。"""
    return semantic_evidence_service.evidence_stats()
