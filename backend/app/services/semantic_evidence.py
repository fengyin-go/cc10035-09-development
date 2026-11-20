"""语义证据数据索引领域服务。

串起三条链路：

1. 语义结果消费「带页面出处的情感记录」时，把预测与原文片段写入证据索引；
   缺出处的预测只登记到标注交付清单，不能被按页检索命中。
2. 运营按页面 + 意图 + 情感标注 / 文本片段检索证据，命中后回情感标注核对原片段。
3. 情感标注补交出处后，索引条目与交付清单一并回写确认，旧编号仍可回查。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.evidence_store import (
    EVIDENCE_CONFIRMED,
    EVIDENCE_PENDING,
    evidence_store,
)
from app.store import store


def build_annotation_version(sentiment_record_id: str, text_snippet: str) -> str:
    """同一条情感记录的同一段文本对应同一个标注版本。"""
    digest = hashlib.sha256(
        f"{sentiment_record_id}|{text_snippet}".encode()
    ).hexdigest()[:10]
    return f"ver-{digest}"


def build_dedup_key(annotation_version: str, page_uri: str, intent: str, text_snippet: str) -> str:
    raw = "|".join([annotation_version, page_uri or "", intent, text_snippet])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_stage_key(annotation_version: str, page_uri: str, intent: str) -> str:
    return hashlib.sha256(
        "|".join([annotation_version, page_uri or "", intent]).encode()
    ).hexdigest()[:16]


class SemanticEvidenceService:
    """语义证据索引：不挂在通用资源路由下，走专用证据路由。"""

    # ---- 写入：语义结果消费情感标注 ------------------------------------

    def index_from_sentiment(
        self,
        *,
        sentiment_record_id: str,
        intent: str,
        nlu_record_id: str = "",
        score: str = "",
    ) -> dict[str, Any]:
        """消费一条情感标注并写入语义证据索引。

        情感记录带「页面出处」时出处直接确认；否则条目保持待确认并进入标注交付清单，
        运营补交出处前不会出现在任何按页检索结果里。
        """
        sentiment_row = next(
            (r for r in store.rows("sentiment_tag") if r.get("记录ID") == sentiment_record_id),
            None,
        )
        if sentiment_row is None:
            return {"ok": False, "reason": "情感记录不存在", "sentiment_record_id": sentiment_record_id}

        text_snippet = str(sentiment_row.get("文本片段", ""))
        page_uri = str(sentiment_row.get("页面出处", "") or "").strip()
        annotation_version = str(
            sentiment_row.get("标注版本")
            or build_annotation_version(sentiment_record_id, text_snippet)
        )
        legacy_aliases = self._collect_legacy_aliases(sentiment_row, nlu_record_id)
        payload = {
            "情感记录编号": sentiment_record_id,
            "标注版本": annotation_version,
            "文本片段": text_snippet,
            "情感倾向": sentiment_row.get("情感倾向", ""),
            "意图标签": intent,
            "语义记录编号": nlu_record_id,
            "预测分数": str(score or ""),
            "页面出处": page_uri,
            "状态": EVIDENCE_CONFIRMED if page_uri else EVIDENCE_PENDING,
            "legacy_aliases": legacy_aliases,
        }
        dedup_key = build_dedup_key(annotation_version, page_uri, intent, text_snippet)
        with evidence_store.lock:
            entry, created = evidence_store.upsert_entry(payload, dedup_key)
            # 重新登记旧编号：兼容历史预测记录里已经存在的编号。
            for alias in legacy_aliases:
                evidence_store.register_legacy_alias(alias, entry["id"])
            # 出处确认后，如果该版本/文本此前还有空出处的待确认占位条目（同文本先写
            # 预测、后补出处的场景），把占位条目合并到这份已确认记录上，保证检索响应
            # 只映射到一份出处记录。
            if entry["状态"] == EVIDENCE_CONFIRMED:
                self._absorb_pending_twin(entry)
            checklist_item = None
            if entry["状态"] != EVIDENCE_CONFIRMED:
                checklist_item = evidence_store.ensure_checklist_item(entry)
        return {
            "ok": True,
            "created": created,
            "evidence": self._public_entry(entry),
            "checklist_item": checklist_item,
        }

    def _collect_legacy_aliases(self, sentiment_row: dict[str, Any], nlu_record_id: str) -> list[str]:
        aliases: list[str] = []
        if nlu_record_id:
            aliases.append(nlu_record_id)
        # 情感记录自身编号以及历史预测里用过的编号都要能回查到同一份出处。
        aliases.append(str(sentiment_row.get("记录ID", "")))
        for field in ("历史预测编号", "旧记录编号"):
            value = sentiment_row.get(field)
            if value:
                aliases.extend(str(value).split(","))
        return [alias.strip() for alias in aliases if alias and alias.strip()]

    # ---- 检索：按页面 + 意图 + 情感标注/文本片段反查 -------------------

    def search_evidence(
        self,
        *,
        page_uri: str = "",
        intent: str = "",
        sentiment_record_id: str = "",
        text_snippet: str = "",
        include_unconfirmed: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        """按原页面证据反查语义预测。

        硬性规则：按页面筛选只返回该页面出处已确认的证据。待确认条目可能属于别的页面
        （同文本在甲乙两页都有预测），绝不能在乙页的检索结果里返回甲页原文。
        """
        rows = evidence_store.entries()
        hits: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        page_key = page_uri.strip()
        snippet_key = text_snippet.strip().casefold()
        for row in rows:
            if intent and row.get("意图标签") != intent:
                continue
            if sentiment_record_id and row.get("情感记录编号") != sentiment_record_id:
                continue
            if snippet_key and snippet_key not in str(row.get("文本片段", "")).casefold():
                continue
            confirmed = row.get("状态") == EVIDENCE_CONFIRMED
            if page_key:
                if not confirmed or row.get("页面出处") != page_key:
                    # 同文本但出处未确认/属于其他页面：显式拦截，提示运营回情感标注核对。
                    blocked.append(self._blocked_view(row, page_key))
                    continue
            elif not include_unconfirmed and not confirmed:
                blocked.append(self._blocked_view(row, page_key))
                continue
            hits.append(self._public_entry(row))

        total = len(hits)
        start = (page - 1) * size
        return {
            "items": hits[start:start + size],
            "total": total,
            "page": page,
            "size": size,
            "filters": {
                "页面出处": page_key,
                "意图标签": intent,
                "情感记录编号": sentiment_record_id,
                "文本片段": text_snippet.strip(),
            },
            "blocked_unconfirmed": blocked,
        }

    def _blocked_view(self, row: dict[str, Any], requested_page: str) -> dict[str, Any]:
        return {
            "证据编号": row.get("证据编号"),
            "情感记录编号": row.get("情感记录编号"),
            "标注版本": row.get("标注版本"),
            "意图标签": row.get("意图标签"),
            "当前状态": row.get("状态"),
            "已确认出处": row.get("页面出处", ""),
            "请求页面": requested_page,
            "原因": "出处尚未在该页面复核确认，不能作为本页证据返回",
        }

    # ---- 补交出处：中断与重投 ------------------------------------------

    def supplement_provenance(
        self,
        *,
        annotation_version: str,
        page_uri: str,
        intent: str = "",
        text_snippet: str = "",
        sentiment_record_id: str = "",
        commit: bool = True,
    ) -> dict[str, Any]:
        """情感标注补交页面出处。

        ``commit=False`` 只暂存（模拟乙页补交中断），重投时先取暂存再提交；
        整个提交在索引锁内完成，同一标注版本的并发提交只会确认同一份出处记录。
        """
        page_uri = page_uri.strip()
        if not page_uri:
            return {"ok": False, "reason": "页面出处不能为空"}

        stage_key = build_stage_key(annotation_version, page_uri, intent)
        if not commit:
            staged = evidence_store.stage_request(stage_key, {
                "标注版本": annotation_version,
                "页面出处": page_uri,
                "意图标签": intent,
                "文本片段": text_snippet,
                "情感记录编号": sentiment_record_id,
            })
            return {"ok": True, "committed": False, "staged": staged}

        staged = evidence_store.get_staged(stage_key)
        if staged is not None:
            intent = intent or str(staged.get("意图标签", ""))
            text_snippet = text_snippet or str(staged.get("文本片段", ""))
            sentiment_record_id = sentiment_record_id or str(staged.get("情感记录编号", ""))

        # 同一标注版本的并发/重复提交串行化：先到的确认出处，后到的幂等返回同一份记录。
        with evidence_store.version_lock(annotation_version):
            already = self._find_confirmed(
                annotation_version=annotation_version,
                page_uri=page_uri,
                intent=intent,
                text_snippet=text_snippet,
                sentiment_record_id=sentiment_record_id,
            )
            if already:
                evidence_store.drop_staged(stage_key)
                return {
                    "ok": True,
                    "committed": True,
                    "idempotent": True,
                    "stage_key": stage_key,
                    "confirmed_count": len(already),
                    "evidence": [self._public_entry(row) for row in already],
                    "checklist_updates": [],
                    "synced_sentiment_records": [],
                }

            with evidence_store.lock:
                targets = self._match_pending_entries(
                    annotation_version=annotation_version,
                    page_uri=page_uri,
                    intent=intent,
                    text_snippet=text_snippet,
                    sentiment_record_id=sentiment_record_id,
                )
                if not targets:
                    evidence_store.drop_staged(stage_key)
                    return {
                        "ok": False,
                        "reason": "没有找到该标注版本下待确认出处的证据条目",
                        "stage_key": stage_key,
                    }

                confirmed_entries: list[dict[str, Any]] = []
                checklist_updates: list[dict[str, Any]] = []
                touched_sentiment_rows: list[dict[str, Any]] = []
                for entry in targets:
                    # 合并流程可能已把该条目并入规范条目，先确认它还在。
                    if evidence_store.find_entry(entry["id"]) is None:
                        continue
                    evidence_store.confirm_entry(entry["id"], page_uri)
                    # 出处变化后迁移去重键；同版本同页同意图同文本只保留一份出处记录，
                    # 重复条目（并发提交/先写预测后补出处）合并到规范条目。
                    self._reindex_and_collapse(entry, page_uri)
                    surviving = evidence_store.find_entry(entry["id"])
                    if surviving is None:
                        # 当前条目已被并入规范条目，清单与情感记录按规范条目回写。
                        surviving = evidence_store.find_by_key(
                            build_dedup_key(
                                str(entry["标注版本"]), page_uri,
                                str(entry["意图标签"]), str(entry["文本片段"]),
                            )
                        )
                    if surviving is None:
                        continue
                    checklist_item = evidence_store.complete_checklist_item(surviving["id"], page_uri)
                    if checklist_item is not None and checklist_item not in checklist_updates:
                        checklist_updates.append(checklist_item)
                    public = self._public_entry(surviving)
                    if public not in confirmed_entries:
                        confirmed_entries.append(public)
                    sentiment_row = self._sync_sentiment_record(surviving, page_uri)
                    if sentiment_row is not None and sentiment_row not in touched_sentiment_rows:
                        touched_sentiment_rows.append(sentiment_row)
                evidence_store.drop_staged(stage_key)

        return {
            "ok": True,
            "committed": True,
            "idempotent": False,
            "stage_key": stage_key,
            "confirmed_count": len(confirmed_entries),
            "evidence": confirmed_entries,
            "checklist_updates": checklist_updates,
            "synced_sentiment_records": [
                {"记录ID": row.get("记录ID"), "页面出处": row.get("页面出处"),
                 "标注版本": row.get("标注版本")}
                for row in touched_sentiment_rows
            ],
        }

    def _find_confirmed(
        self,
        *,
        annotation_version: str,
        page_uri: str,
        intent: str,
        text_snippet: str,
        sentiment_record_id: str,
    ) -> list[dict[str, Any]]:
        """幂等回查：并发提交里后到的请求直接复用已确认的同页出处记录。"""
        snippet_key = text_snippet.strip().casefold()
        matches: list[dict[str, Any]] = []
        for row in evidence_store.entries():
            if row.get("标注版本") != annotation_version:
                continue
            if row.get("状态") != EVIDENCE_CONFIRMED or row.get("页面出处") != page_uri:
                continue
            if intent and row.get("意图标签") != intent:
                continue
            if sentiment_record_id and row.get("情感记录编号") != sentiment_record_id:
                continue
            if snippet_key and snippet_key not in str(row.get("文本片段", "")).casefold():
                continue
            matches.append(row)
        return matches

    def _match_pending_entries(
        self,
        *,
        annotation_version: str,
        page_uri: str,
        intent: str,
        text_snippet: str,
        sentiment_record_id: str,
    ) -> list[dict[str, Any]]:
        snippet_key = text_snippet.strip().casefold()
        targets: list[dict[str, Any]] = []
        for row in evidence_store.entries():
            if row.get("标注版本") != annotation_version:
                continue
            if row.get("状态") == EVIDENCE_CONFIRMED:
                continue
            if intent and row.get("意图标签") != intent:
                continue
            if sentiment_record_id and row.get("情感记录编号") != sentiment_record_id:
                continue
            if snippet_key and snippet_key not in str(row.get("文本片段", "")).casefold():
                continue
            targets.append(row)
        return targets

    def _reindex_and_collapse(self, entry: dict[str, Any], page_uri: str) -> None:
        """出处确认后迁移去重键，并合并同版本同页同意图同文本的重复条目。"""
        old_key = build_dedup_key(
            str(entry["标注版本"]), "", str(entry["意图标签"]), str(entry["文本片段"]),
        )
        canonical_key = build_dedup_key(
            str(entry["标注版本"]), page_uri,
            str(entry["意图标签"]), str(entry["文本片段"]),
        )
        canonical = evidence_store.find_by_key(canonical_key)
        if canonical is not None and canonical["id"] != entry["id"]:
            # 规范条目已存在（并发提交兜底）：把当前条目的旧编号并入后删除自身。
            for alias in evidence_store.aliases_for(entry["id"]):
                evidence_store.register_legacy_alias(alias, canonical["id"])
            evidence_store.remove_entry(entry["id"])
            return
        evidence_store.reindex_key(old_key, canonical_key, entry["id"])
        self._absorb_pending_twin(evidence_store.find_entry(entry["id"]) or entry)

    def _absorb_pending_twin(self, confirmed_entry: dict[str, Any]) -> None:
        """把同版本/同文本/同意图的空出处待确认占位条目合并到已确认条目。"""
        twins = [
            row for row in evidence_store.entries()
            if row["id"] != confirmed_entry["id"]
            and row.get("状态") != EVIDENCE_CONFIRMED
            and row.get("标注版本") == confirmed_entry.get("标注版本")
            and row.get("文本片段") == confirmed_entry.get("文本片段")
            and row.get("意图标签") == confirmed_entry.get("意图标签")
        ]
        for twin in twins:
            for alias in evidence_store.aliases_for(twin["id"]):
                evidence_store.register_legacy_alias(alias, confirmed_entry["id"])
            evidence_store.remove_entry(twin["id"])

    def _sync_sentiment_record(self, entry: dict[str, Any], page_uri: str) -> dict[str, Any] | None:
        """索引确认结果回写到情感标注记录本身。"""
        sentiment_row = next(
            (r for r in store.rows("sentiment_tag")
             if r.get("记录ID") == entry.get("情感记录编号")),
            None,
        )
        if sentiment_row is None:
            return None
        sentiment_row["页面出处"] = page_uri
        sentiment_row.setdefault("标注版本", entry.get("标注版本"))
        return sentiment_row

    # ---- 旧编号回查 -----------------------------------------------------

    def resolve_legacy_id(self, legacy_id: str) -> dict[str, Any]:
        entry = evidence_store.resolve_alias(legacy_id)
        if entry is None:
            # 也允许直接用证据编号查。
            entry = next(
                (row for row in evidence_store.entries() if row.get("证据编号") == legacy_id),
                None,
            )
        if entry is None:
            return {"ok": False, "reason": "旧编号未映射到任何出处记录", "legacy_id": legacy_id}
        public = self._public_entry(entry)
        return {
            "ok": True,
            "legacy_id": legacy_id,
            "evidence_id": entry["id"],
            "aliases": evidence_store.aliases_for(entry["id"]),
            "evidence": public,
        }

    # ---- 标注交付清单与统计 ---------------------------------------------

    def delivery_checklist(self) -> dict[str, Any]:
        items = evidence_store.checklist_items()
        return {"items": items, "total": len(items)}

    def evidence_stats(self) -> dict[str, Any]:
        """全站计数与逐页出处。"""
        rows = evidence_store.entries()
        confirmed = [row for row in rows if row.get("状态") == EVIDENCE_CONFIRMED]
        pending = [row for row in rows if row.get("状态") != EVIDENCE_CONFIRMED]
        page_breakdown: dict[str, dict[str, Any]] = {}
        for row in confirmed:
            page = str(row.get("页面出处") or "")
            bucket = page_breakdown.setdefault(page, {"页面出处": page, "证据数": 0, "意图分布": {}})
            bucket["证据数"] += 1
            intent = str(row.get("意图标签") or "未知")
            bucket["意图分布"][intent] = bucket["意图分布"].get(intent, 0) + 1
        return {
            "total_evidence": len(rows),
            "confirmed_evidence": len(confirmed),
            "pending_evidence": len(pending),
            "covered_pages": len(page_breakdown),
            "checklist_pending": len(evidence_store.checklist_items()),
            "legacy_aliases": len(evidence_store.all_aliases()),
            "page_breakdown": sorted(page_breakdown.values(), key=lambda item: item["页面出处"]),
        }

    # ---- 视图模型 -------------------------------------------------------

    def _public_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        data = {key: value for key, value in entry.items() if key != "legacy_aliases"}
        data["历史编号"] = evidence_store.aliases_for(entry["id"])
        return data


semantic_evidence_service = SemanticEvidenceService()
