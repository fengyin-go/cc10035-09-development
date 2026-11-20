"""语义证据数据索引端到端场景验证。

覆盖验收路径：
1. 同一文本录入甲乙两页的情感标注；
2. 语义结果消费情感标注，写入证据索引，缺出处条目进入标注交付清单；
3. 乙页补交出处中断（仅暂存）——中断前后按乙页 + 意图检索都不能拿到不可复核的证据；
4. 重投确认后再次按乙页 + 同一意图检索，从命中回查原片段与交付清单；
5. 并发提交同一标注版本只映射一份出处记录；
6. 历史预测旧编号仍可回查；
7. 全站计数与逐页出处统计。

直接调用服务层（不依赖 httpx），另用 uvicorn + urllib 做一组真实 HTTP 冒烟。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request

sys.path.insert(0, "/workspace/backend")

from app.evidence_store import evidence_store  # noqa: E402
from app.services.nlu_result import NluResultService  # noqa: E402
from app.services.semantic_evidence import (  # noqa: E402
    build_annotation_version,
    semantic_evidence_service as svc,
)
from app.store import store  # noqa: E402

PAGE_A = "https://shop.example.com/a"
PAGE_B = "https://shop.example.com/b"
SHARED_TEXT = "物流太慢了，包装倒是完好，希望售后能跟进"
INTENT = "投诉反馈"

failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def reset_state() -> None:
    evidence_store.reset()
    store._tables["sentiment_tag"] = []
    store._tables["nlu_result"] = []


def add_sentiment(record_id: str, page_uri: str, legacy_id: str = "") -> dict:
    row = {
        "id": max((int(r.get("id", 0)) for r in store.rows("sentiment_tag")), default=0) + 1,
        "status": "已标注",
        "pending": True,
        "abnormal": False,
        "记录ID": record_id,
        "文本片段": SHARED_TEXT,
        "情感倾向": "mixed",
        "强烈程度": "中",
        "方面词": "交付",
        "方面情感": "negative",
        "标注模型": "sentiment-analyzer-v2",
        "页面出处": page_uri,
        "标注版本": build_annotation_version(record_id, SHARED_TEXT),
        "历史预测编号": legacy_id,
    }
    store.rows("sentiment_tag").append(row)
    return row


def run_service_scenario() -> None:
    print("== 场景一：同文本甲乙两页预测，乙页出处待补，按页检索隔离 ==")
    reset_state()

    # 甲页情感记录带出处；乙页情感记录先不给出处（预测先写成、出处未确认）。
    row_a = add_sentiment("sent-page-a", PAGE_A, legacy_id="nlu-legacy-001")
    row_b = add_sentiment("sent-page-b", "", legacy_id="nlu-legacy-002")
    version_b = row_b["标注版本"]

    nlu = NluResultService()
    result_a = nlu.consume_sentiment_and_segment("sent-page-a")
    intent_a = result_a["意图标签"]
    check("甲页消费后证据已索引", result_a["evidence_indexed"] is True)
    check("甲页出处直接确认", result_a["provenance_confirmed"] is True)
    nlu_id_b = nlu.nlu_predict(SHARED_TEXT)["记录ID"]
    indexed_b = svc.index_from_sentiment(
        sentiment_record_id="sent-page-b", intent=INTENT, nlu_record_id=nlu_id_b,
    )
    check("乙页证据已写入索引", indexed_b["ok"] is True)
    check("乙页证据保持待确认出处", indexed_b["evidence"]["状态"] == "待确认出处")
    check("乙页缺出处条目进入标注交付清单", indexed_b["checklist_item"] is not None)

    # 按乙页 + 意图检索：不能命中，也不能错用甲页原文。
    search_b = svc.search_evidence(page_uri=PAGE_B, intent=INTENT)
    check("乙页检索无已确认证据", search_b["total"] == 0, search_b)
    check("待确认条目被显式拦截而非静默丢弃",
          any(item["情感记录编号"] == "sent-page-b" for item in search_b["blocked_unconfirmed"]))
    check("拦截结果不泄漏尚不能在乙页复核的文本片段",
          all("文本片段" not in item for item in search_b["blocked_unconfirmed"]))

    # 按甲页 + 甲页实际意图检索：只能拿到甲页自己的出处。
    search_a = svc.search_evidence(page_uri=PAGE_A, intent=intent_a)
    check("甲页检索只命中甲页出处",
          search_a["total"] == 1 and search_a["items"][0]["页面出处"] == PAGE_A)
    check("甲页命中可回查原始文本片段", search_a["items"][0]["文本片段"] == SHARED_TEXT)
    check("甲页命中可回查情感标注编号", search_a["items"][0]["情感记录编号"] == "sent-page-a")

    # 按文本片段跨页检索时，待确认条目也不能混进乙页结果。
    search_text_b = svc.search_evidence(page_uri=PAGE_B, text_snippet=SHARED_TEXT)
    blocked_b = {item["情感记录编号"]: item for item in search_text_b["blocked_unconfirmed"]}
    check("同文本按乙页检索无已确认命中", search_text_b["total"] == 0)
    check("甲页已确认证据不会被错放到乙页命中里", search_text_b["total"] == 0)
    check("乙页待确认条目在乙页检索中被拦截并提示回情感标注核对",
          "sent-page-b" in blocked_b
          and blocked_b["sent-page-b"]["当前状态"] == "待确认出处"
          and "文本片段" not in blocked_b["sent-page-b"])
    check("甲页证据只以「出处属于其他页面」形式出现且不返回原文",
          "sent-page-a" in blocked_b
          and blocked_b["sent-page-a"]["已确认出处"] == PAGE_A
          and "文本片段" not in blocked_b["sent-page-a"])
    # 按文本片段在甲页检索时，乙页的待确认条目不能伪装成甲页证据。
    search_text_a = svc.search_evidence(page_uri=PAGE_A, text_snippet=SHARED_TEXT)
    check("同文本按甲页检索只返回甲页已确认证据",
          search_text_a["total"] == 1 and search_text_a["items"][0]["情感记录编号"] == "sent-page-a")

    print()
    print("== 场景二：乙页补交出处中断（仅暂存），重投前后检索行为一致 ==")
    stage = svc.supplement_provenance(
        annotation_version=version_b, page_uri=PAGE_B, intent=INTENT,
        text_snippet=SHARED_TEXT, sentiment_record_id="sent-page-b", commit=False,
    )
    check("中断补交只暂存不确认", stage["committed"] is False and stage["ok"] is True)
    search_b_mid = svc.search_evidence(page_uri=PAGE_B, intent=INTENT)
    check("中断后乙页仍无可复核证据", search_b_mid["total"] == 0)
    check("中断期间交付清单仍挂着乙页条目",
          any(item["情感记录编号"] == "sent-page-b" for item in svc.delivery_checklist()["items"]))

    # 重投确认。
    commit = svc.supplement_provenance(
        annotation_version=version_b, page_uri=PAGE_B, intent=INTENT,
        text_snippet=SHARED_TEXT, sentiment_record_id="sent-page-b", commit=True,
    )
    check("重投后出处确认成功", commit["ok"] is True and commit["committed"] is True)
    check("确认条目数为一份", commit["confirmed_count"] == 1, commit)
    check("情感记录回写页面出处",
          next(r for r in store.rows("sentiment_tag") if r["记录ID"] == "sent-page-b")["页面出处"] == PAGE_B)
    check("交付清单回写为已确认（清单不再挂起）",
          not any(item["情感记录编号"] == "sent-page-b" for item in svc.delivery_checklist()["items"]))
    check("暂存请求在确认后清理", evidence_store.get_staged(stage["staged"]["stage_key"]) is None)

    search_b_after = svc.search_evidence(page_uri=PAGE_B, intent=INTENT)
    check("重投后乙页检索命中一份证据", search_b_after["total"] == 1)
    hit = search_b_after["items"][0]
    check("命中证据出处为乙页", hit["页面出处"] == PAGE_B)
    check("从命中回查原始文本片段", hit["文本片段"] == SHARED_TEXT)
    check("从命中回查情感标注编号", hit["情感记录编号"] == "sent-page-b")
    check("从命中回查标注版本", hit["标注版本"] == version_b)

    print()
    print("== 场景三：同标注版本并发提交只映射一份出处记录 ==")
    reset_state()
    row_c = add_sentiment("sent-conc", "")
    version_c = row_c["标注版本"]
    svc.index_from_sentiment(sentiment_record_id="sent-conc", intent=INTENT)
    barrier = threading.Barrier(5)
    results: list[dict] = []

    def worker() -> None:
        barrier.wait()
        results.append(svc.supplement_provenance(
            annotation_version=version_c, page_uri=PAGE_B, intent=INTENT,
            text_snippet=SHARED_TEXT, sentiment_record_id="sent-conc", commit=True,
        ))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("五个并发提交全部成功", all(r["ok"] for r in results))
    check("仅一份提交实际确认，其余幂等返回",
          sum(1 for r in results if not r.get("idempotent")) == 1
          and sum(1 for r in results if r.get("idempotent")) == 4,
          [(r["ok"], r.get("idempotent")) for r in results])
    check("所有并发提交回指同一份出处记录",
          len({r["evidence"][0]["id"] for r in results}) == 1)
    entries = [e for e in evidence_store.entries() if e["标注版本"] == version_c]
    check("索引中该版本只有一份出处记录", len(entries) == 1, len(entries))
    check("该份记录出处已确认", entries[0]["状态"] == "出处已确认" and entries[0]["页面出处"] == PAGE_B)
    stats = svc.evidence_stats()
    check("全站证据总数为 1", stats["total_evidence"] == 1, stats)

    print()
    print("== 场景四：历史预测旧编号回查 ==")
    # 场景一二的 nlu-legacy-002 与 nlu_id_b 都应能回查到乙页那份证据。
    reset_state()
    row_a2 = add_sentiment("sent-legacy-a", PAGE_A, legacy_id="old-pred-777")
    svc.index_from_sentiment(sentiment_record_id="sent-legacy-a", intent=INTENT,
                             nlu_record_id="nlu-new-888")
    legacy = svc.resolve_legacy_id("old-pred-777")
    check("历史预测编号可回查出处记录", legacy["ok"] is True)
    check("旧编号回查到甲页出处", legacy["evidence"]["页面出处"] == PAGE_A)
    check("回查结果携带全部兼容编号",
          set(legacy["aliases"]) >= {"old-pred-777", "nlu-new-888", "sent-legacy-a"})
    new_id = svc.resolve_legacy_id("nlu-new-888")
    check("新语义记录编号回查到同一份记录",
          new_id["ok"] and new_id["evidence_id"] == legacy["evidence_id"])
    missing = svc.resolve_legacy_id("does-not-exist")
    check("未知编号给出明确未映射结果", missing["ok"] is False)

    print()
    print("== 场景五：全站计数与逐页出处 ==")
    reset_state()
    add_sentiment("s1", PAGE_A)
    add_sentiment("s2", PAGE_A)
    add_sentiment("s3", PAGE_B)
    add_sentiment("s4", "")  # 待确认
    for rid, intent in [("s1", INTENT), ("s2", "查询产品信息"), ("s3", INTENT)]:
        svc.index_from_sentiment(sentiment_record_id=rid, intent=intent)
    pending_idx = svc.index_from_sentiment(sentiment_record_id="s4", intent=INTENT)
    stats = svc.evidence_stats()
    check("全站证据计数=4（含待确认）", stats["total_evidence"] == 4, stats)
    check("已确认计数=3", stats["confirmed_evidence"] == 3)
    check("待确认计数=1", stats["pending_evidence"] == 1)
    check("覆盖页面数=2", stats["covered_pages"] == 2)
    check("交付清单待补数=1", stats["checklist_pending"] == 1)
    pages = {p["页面出处"]: p for p in stats["page_breakdown"]}
    check("逐页出处含甲页且证据数=2", pages.get(PAGE_A, {}).get("证据数") == 2)
    check("逐页出处含乙页且证据数=1", pages.get(PAGE_B, {}).get("证据数") == 1)
    check("甲页意图分布正确", pages[PAGE_A]["意图分布"].get(INTENT) == 1
          and pages[PAGE_A]["意图分布"].get("查询产品信息") == 1)
    check("待确认条目不出现在任何逐页出处中",
          all(p["页面出处"] for p in stats["page_breakdown"]))

    print()
    print("== 场景六：出处补齐后重新消费，待确认占位被合并为一份确认记录 ==")
    reset_state()
    row = add_sentiment("sent-reindex", "")
    version = row["标注版本"]
    first = svc.index_from_sentiment(sentiment_record_id="sent-reindex", intent=INTENT)
    check("首次消费生成待确认条目", first["evidence"]["状态"] == "待确认出处")
    check("首次消费进入交付清单", svc.delivery_checklist()["total"] == 1)
    # 情感记录出处被修正后重新消费同一版本。
    next(r for r in store.rows("sentiment_tag") if r["记录ID"] == "sent-reindex")["页面出处"] = PAGE_A
    second = svc.index_from_sentiment(sentiment_record_id="sent-reindex", intent=INTENT,
                                      nlu_record_id="pred-reindex-1")
    check("重新消费后出处已确认", second["evidence"]["状态"] == "出处已确认")
    same_version = [e for e in evidence_store.entries() if e["标注版本"] == version]
    check("同版本只剩一份出处记录（占位被合并）", len(same_version) == 1, len(same_version))
    check("合并后交付清单清空", svc.delivery_checklist()["total"] == 0)
    check("旧编号跟随合并到确认记录",
          "pred-reindex-1" in evidence_store.aliases_for(same_version[0]["id"]))


def run_http_smoke(base: str = "http://127.0.0.1:8000") -> None:
    print()
    print("== HTTP 冒烟：真实 uvicorn 接口 ==")

    def get(path: str) -> dict:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    stats = get("/api/semantic-evidence/stats")
    check("HTTP 统计接口可用", "total_evidence" in stats)
    checklist = get("/api/semantic-evidence/checklist")
    check("HTTP 交付清单接口可用", "items" in checklist and "total" in checklist)
    search = get("/api/semantic-evidence/search?page_uri=" + urllib.parse.quote(PAGE_A)
                 + "&intent=" + urllib.parse.quote(INTENT))
    check("HTTP 检索接口返回分页结构", {"items", "total", "page", "size"} <= set(search))
    # 固定路径优先于 /api/{module} 动态路由。
    bad = get("/api/semantic-evidence/legacy/nlu-legacy-001")
    check("HTTP 旧编号回查接口可用且未被动态路由截获", "legacy_id" in bad)


if __name__ == "__main__":
    run_service_scenario()
    if "--http" in sys.argv:
        run_http_smoke()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部断言通过")
