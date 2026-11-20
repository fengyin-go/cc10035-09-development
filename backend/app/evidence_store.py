"""语义证据数据索引的内存仓库。

与业务模块的 ``store`` 分开存放：证据索引是情感标注 → 语义结果链路上的派生数据，
不进入 18 个业务模块的运营概览，但同样在进程内可筛选、可流转。

关键设计：

* 证据条目按「标注版本 + 页面出处 + 意图标签 + 文本片段」去重，同一标注版本被
  并发/重复提交时只映射到同一份出处记录（upsert + 锁）。
* 出处未确认的条目不允许进入按页检索结果，避免同一段文本在甲乙两页都有预测时，
  乙页错用甲页已确认的原文。
* ``legacy_aliases`` 保留历史预测记录里的旧编号，补交出处后仍能从旧编号回查。
* 标注交付清单（delivery checklist）只登记缺出处的证据条目，补交后回写确认状态。
"""
from __future__ import annotations

import threading
from typing import Any


# 证据条目状态：出处确认前后的生命周期
EVIDENCE_PENDING = "待确认出处"
EVIDENCE_CONFIRMED = "出处已确认"

# 标注交付清单状态
CHECKLIST_PENDING = "待补出处"
CHECKLIST_DONE = "出处已确认"


class EvidenceStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 证据条目（按写入顺序排列，id 单调递增）
        self._entries: list[dict[str, Any]] = []
        # 去重键 -> 证据条目 id
        self._key_index: dict[str, int] = {}
        # 历史编号（旧预测编号/记录编号/证据编号）-> 证据条目 id
        self._legacy_aliases: dict[str, int] = {}
        # 标注交付清单：证据条目 id -> 清单行
        self._checklist: dict[int, dict[str, Any]] = {}
        # 中断重投前的暂存请求（按标注版本 + 页面出处归并）
        self._staged: dict[str, dict[str, Any]] = {}
        # 同一标注版本的补交出处串行化，保证并发提交只确认一份出处记录。
        self._version_locks: dict[str, threading.Lock] = {}
        self._next_id = 1

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def version_lock(self, annotation_version: str) -> threading.Lock:
        with self._lock:
            version_lock = self._version_locks.get(annotation_version)
            if version_lock is None:
                version_lock = threading.Lock()
                self._version_locks[annotation_version] = version_lock
            return version_lock

    # ---- 证据条目 -------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def find_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            return next((row for row in self._entries if row["id"] == entry_id), None)

    def find_by_key(self, dedup_key: str) -> dict[str, Any] | None:
        with self._lock:
            entry_id = self._key_index.get(dedup_key)
            return self.find_entry(entry_id) if entry_id is not None else None

    def upsert_entry(self, payload: dict[str, Any], dedup_key: str) -> tuple[dict[str, Any], bool]:
        """按去重键写入证据条目，返回 (条目, 是否新建)。

        同一标注版本并发提交时，后来的提交只会刷新可变信息，不会生成第二份出处记录。
        """
        with self._lock:
            existing_id = self._key_index.get(dedup_key)
            if existing_id is not None:
                entry = self.find_entry(existing_id)
                assert entry is not None
                # 已确认的出处不允许被后来的未确认提交覆盖；只更新预测侧的可变字段。
                entry["预测分数"] = payload.get("预测分数", entry.get("预测分数", ""))
                entry["语义记录编号"] = payload.get("语义记录编号", entry.get("语义记录编号", ""))
                for alias in payload.get("legacy_aliases", []):
                    self._register_alias(alias, existing_id)
                return entry, False

            entry = {"id": self._next_id}
            self._next_id += 1
            entry.update(payload)
            entry.setdefault("证据编号", f"evi-{entry['id']:04d}")
            entry.setdefault("状态", EVIDENCE_PENDING)
            self._entries.append(entry)
            self._key_index[dedup_key] = entry["id"]
            for alias in payload.get("legacy_aliases", []):
                self._register_alias(alias, entry["id"])
            return entry, True

    def confirm_entry(self, entry_id: int, page_uri: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self.find_entry(entry_id)
            if entry is None:
                return None
            entry["页面出处"] = page_uri
            entry["状态"] = EVIDENCE_CONFIRMED
            return entry

    def reindex_key(self, old_key: str, new_key: str, entry_id: int) -> None:
        """出处确认后去重键随页面出处变化，迁移键索引；已占用的新键保持不动。"""
        with self._lock:
            if self._key_index.get(old_key) == entry_id:
                self._key_index.pop(old_key, None)
            self._key_index.setdefault(new_key, entry_id)

    def remove_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self.find_entry(entry_id)
            if entry is None:
                return None
            self._entries = [row for row in self._entries if row["id"] != entry_id]
            for key, target in list(self._key_index.items()):
                if target == entry_id:
                    self._key_index.pop(key, None)
            self._checklist.pop(entry_id, None)
            return entry

    # ---- 历史编号兼容 ---------------------------------------------------

    def _register_alias(self, alias: str, entry_id: int) -> None:
        alias = str(alias).strip()
        if alias:
            # 已有映射保持稳定：旧编号永远指向第一次落到的那份出处记录。
            self._legacy_aliases.setdefault(alias, entry_id)

    def register_legacy_alias(self, alias: str, entry_id: int) -> None:
        self._register_alias(alias, entry_id)

    def resolve_alias(self, legacy_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry_id = self._legacy_aliases.get(str(legacy_id).strip())
            if entry_id is None:
                return None
            return self.find_entry(entry_id)

    def aliases_for(self, entry_id: int) -> list[str]:
        with self._lock:
            return sorted(alias for alias, target in self._legacy_aliases.items() if target == entry_id)

    def all_aliases(self) -> list[str]:
        with self._lock:
            return list(self._legacy_aliases.keys())

    # ---- 标注交付清单 ---------------------------------------------------

    def ensure_checklist_item(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """缺页面出处的证据条目进入交付清单；已确认的条目不占位。"""
        with self._lock:
            entry_id = entry["id"]
            if entry.get("状态") == EVIDENCE_CONFIRMED:
                self._checklist.pop(entry_id, None)
                return None
            existing = self._checklist.get(entry_id)
            if existing is not None:
                return existing
            item = {
                "id": entry_id,
                "证据编号": entry.get("证据编号"),
                "情感记录编号": entry.get("情感记录编号"),
                "标注版本": entry.get("标注版本"),
                "意图标签": entry.get("意图标签"),
                "文本片段": entry.get("文本片段"),
                "页面出处": "",
                "清单状态": CHECKLIST_PENDING,
            }
            self._checklist[entry_id] = item
            return item

    def complete_checklist_item(self, entry_id: int, page_uri: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._checklist.get(entry_id)
            if item is None:
                return None
            item["页面出处"] = page_uri
            item["清单状态"] = CHECKLIST_DONE
            return item

    def checklist_items(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._checklist.values() if item["清单状态"] == CHECKLIST_PENDING]

    # ---- 中断 / 重投 ----------------------------------------------------

    def stage_request(self, stage_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            staged = dict(payload)
            staged["stage_key"] = stage_key
            self._staged[stage_key] = staged
            return dict(staged)

    def get_staged(self, stage_key: str) -> dict[str, Any] | None:
        with self._lock:
            staged = self._staged.get(stage_key)
            return dict(staged) if staged is not None else None

    def drop_staged(self, stage_key: str) -> None:
        with self._lock:
            self._staged.pop(stage_key, None)

    def staged_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._staged.values()]

    def reset(self) -> None:
        """测试辅助：清空全部索引数据。"""
        with self._lock:
            self._entries.clear()
            self._key_index.clear()
            self._legacy_aliases.clear()
            self._checklist.clear()
            self._staged.clear()
            self._next_id = 1


evidence_store = EvidenceStore()
