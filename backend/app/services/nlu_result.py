"""语义结果(NLU)领域服务：管理自然语言理解的语义结构化输出。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


INTENT_LABELS = [
    "查询产品信息", "对比价格", "咨询售后", "投诉反馈",
    "订单查询", "物流追踪", "功能咨询", "操作指导", "通用对话",
]
SLOT_TYPES = ["product_name", "order_id", "date_range", "price_range", "contact_method"]


class NluResultService(BaseModuleService):
    MODULE = "nlu_result"
    ENTITY = "语义记录"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["记录ID", "输入文本", "意图标签", "槽位列表", "语义框架", "语言模型", "预测分数"]
    REQUIRED_FIELDS = ["记录ID", "输入文本", "意图标签"]
    STATUS_ORDER = ["已预测", "已验证", "已入库", "已驳回"]
    ACTION_RULES = {"验证结果": "已预测", "确认入库": "已验证", "驳回结果": "已预测"}
    NEGATIVE_ACTIONS = ["驳回结果"]

    def nlu_predict(self, text_chunk: str, source_page: str = "") -> dict[str, Any]:
        """对文本块做NLU预测：消费情感标注和文本分块的输出。"""
        text_hash = int(hashlib.md5(text_chunk.encode()).hexdigest()[:4], 16)
        intent_idx = text_hash % len(INTENT_LABELS)
        intent = INTENT_LABELS[intent_idx]
        slots = {}
        for slot_type in SLOT_TYPES:
            slot_hash = int(hashlib.md5(f"{text_chunk}|{slot_type}".encode()).hexdigest()[:4], 16)
            if slot_hash % 3 == 0:
                continue
            slots[slot_type] = f"value_from_{intent_idx}_{slot_hash % 100}"
        score = round(0.65 + (text_hash % 300) / 1000.0, 2)
        record_id = hashlib.sha256(text_chunk.encode()).hexdigest()[:12]

        return {
            "记录ID": record_id,
            "输入文本": text_chunk[:200],
            "意图标签": intent,
            "槽位列表": str(slots),
            "语义框架": "intent_slot_framework_v3",
            "语言模型": "nlu-transformer-zh",
            "预测分数": str(score),
        }

    def consume_sentiment_and_segment(self, sentiment_record_id: str, source_page: str = "") -> dict[str, Any]:
        """消费情感标注的输出，接入NLU预测链路，并写入语义证据数据索引。

        情感记录带页面出处（或调用方显式给出 source_page）时，证据条目出处直接确认；
        否则只登记到标注交付清单，运营补交出处前不能按页检索到该证据。
        """
        sentiment_row = next(
            (r for r in store.rows("sentiment_tag") if r.get("记录ID") == sentiment_record_id),
            None
        )
        if sentiment_row is None:
            return {"ok": False, "reason": "情感记录不存在"}

        text = sentiment_row.get("文本片段", "")
        sentiment = sentiment_row.get("情感倾向", "neutral")
        nlu_result = self.nlu_predict(text)
        nlu_result["source_sentiment"] = sentiment
        nlu_result["source_intensity"] = sentiment_row.get("强烈程度", "")
        if source_page:
            # 调用方显式指定出处时回写情感记录，保证后续消费同版本记录出处一致。
            sentiment_row["页面出处"] = source_page
        # 延迟导入，避免服务注册阶段的循环依赖。
        from app.services.semantic_evidence import semantic_evidence_service
        evidence = semantic_evidence_service.index_from_sentiment(
            sentiment_record_id=sentiment_record_id,
            intent=str(nlu_result["意图标签"]),
            nlu_record_id=str(nlu_result["记录ID"]),
            score=str(nlu_result["预测分数"]),
        )
        nlu_result["evidence_indexed"] = evidence.get("ok", False)
        nlu_result["evidence_id"] = (
            evidence.get("evidence", {}).get("证据编号") if evidence.get("ok") else ""
        )
        nlu_result["provenance_confirmed"] = (
            evidence.get("evidence", {}).get("状态") == "出处已确认"
            if evidence.get("ok") else False
        )
        return nlu_result

    def compute_nlu_stats(self) -> dict[str, Any]:
        """NLU统计指标。"""
        rows = store.rows(self.MODULE)
        intent_dist: dict[str, int] = {}
        total_score = 0.0
        for row in rows:
            intent = row.get("意图标签", "未知")
            intent_dist[intent] = intent_dist.get(intent, 0) + 1
            try:
                total_score += float(row.get("预测分数", "0"))
            except (ValueError, TypeError):
                pass
        return {
            "total_predictions": len(rows),
            "intent_distribution": intent_dist,
            "avg_score": round(total_score / len(rows), 2) if rows else 0.0,
            "validated_count": sum(1 for r in rows if r.get("status") == "已验证"),
            "ingested_count": sum(1 for r in rows if r.get("status") == "已入库"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["nlu_stats"] = self.compute_nlu_stats()
        return base