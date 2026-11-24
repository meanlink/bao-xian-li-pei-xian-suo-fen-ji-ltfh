"""合作方按批报案业务逻辑。

关键不变量：
1. 整批先校验后受理，任一内容冲突/字段错误则整批退回，不产生案件；
2. (报案号, 修订号) 唯一确定一份内容，同键不同内容整批退回，同键同内容幂等；
3. 批次号是重试幂等键，重复批次不重复立案；
4. 旧修订不覆盖当前修订；
5. 已分级案件收到新修订时只登记人工确认队列，不直接改案件；
   受理事务与分级事务都锁定 claims 行，锁内复检分级状态，并发下不变量仍成立。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from src.utils.audit import record_in_transaction
from src.utils.db import get_conn

# 与 migrations 中的咨询锁段保持同一编号体系（002/003 迁移用 1045002，seed 用 1045003）。
BATCH_SERIAL_LOCK = 1045010

DEFAULT_DOCUMENTS = ["身份证明", "事故证明", "损失清单"]
GRADED_STATUSES = ("modeled", "investigating", "adjudicating", "closed")

STATUS_LABELS = {
    "created": "新立案",
    "updated": "修订已受理",
    "duplicate": "重复报送",
    "stale_revision": "旧修订忽略",
    "pending_confirmation": "待人工确认",
    "conflict": "内容冲突",
    "invalid": "校验不通过",
    "not_processed": "整批退回未处理",
    "pending": "待人工确认",
    "confirmed": "人工确认已采纳",
    "rejected": "人工确认已退回",
    "superseded": "已被更新修订替代",
    "applied": "已生效",
    "accepted": "已受理",
}

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "report_no": ("report_no", "reportNo", "report_number", "reportNumber", "case_report_no",
                  "claim_report_no", "report_id", "报案号", "报案编号"),
    "revision_no": ("revision_no", "revisionNo", "revision_number", "revisionNumber",
                    "revision", "rev", "version_no", "versionNo", "version", "修订号", "版本号"),
    "accident_no": ("accident_no", "accidentNo", "accident_id", "accidentId", "事故号", "事故编号"),
    "accident_desc": ("accident_desc", "accidentDesc", "accident_description", "accidentDescription",
                      "accident_detail", "事故描述", "事故经过", "事故说明"),
    "policy_no": ("policy_no", "policyNo", "policy_number", "policyNumber", "policy", "保单号", "保单编号"),
    "customer_name": ("customer_name", "customerName", "customer", "insured_name", "insuredName",
                      "客户姓名", "客户", "被保险人"),
    "incident_type": ("incident_type", "incidentType", "claim_type", "claimType",
                      "accident_type", "accidentType", "出险类型", "事故类型", "案件类型"),
    "incident_date": ("incident_date", "incidentDate", "accident_date", "accidentDate",
                      "loss_date", "lossDate", "date_of_incident", "出险日期", "事故日期"),
    "loss_amount": ("loss_amount", "lossAmount", "estimated_loss", "estimatedLoss",
                    "amount", "loss", "估损金额", "损失金额", "金额"),
    "region": ("region", "area", "district", "city", "出险地区", "地区", "区域"),
    "claimant_phone": ("claimant_phone", "claimantPhone", "phone", "contact_phone",
                       "contactPhone", "mobile", "电话", "联系电话", "报案电话"),
    "required_documents": ("required_documents", "requiredDocuments", "documents", "required_docs"),
}

REQUIRED_FIELDS = (
    "report_no", "revision_no", "policy_no", "customer_name",
    "incident_type", "incident_date", "loss_amount", "region", "claimant_phone",
)

FIELD_CN = {
    "report_no": "报案号", "revision_no": "修订号", "policy_no": "保单号",
    "customer_name": "客户姓名", "incident_type": "出险类型", "incident_date": "出险日期",
    "loss_amount": "损失金额", "region": "出险地区", "claimant_phone": "联系电话",
}


class BatchNumberConflict(PermissionError):
    """同一批次号被用于不同内容，或已受理批次号被改写。"""


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pick(raw: dict, field: str):
    for key in FIELD_ALIASES[field]:
        value = raw.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        # 兼容 2026-11-1 这类单位数月日
        parts = text.split("-")
        if len(parts) == 3:
            try:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                return None
    return None


def normalize_item(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """把任意命名风格的一条报案归一化；返回 (item, errors)。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["报案条目必须是对象"]

    item: dict[str, Any] = {}
    report_no = _pick(raw, "report_no")
    item["report_no"] = str(report_no).strip() if report_no is not None else ""
    if not item["report_no"]:
        errors.append("缺少报案号")

    rev_value = _pick(raw, "revision_no")
    revision_no: int | None = None
    if isinstance(rev_value, bool):
        errors.append("修订号必须是不小于 1 的整数")
    else:
        try:
            revision_no = int(rev_value)  # type: ignore[arg-type]
            if revision_no < 1:
                errors.append("修订号必须不小于 1")
                revision_no = None
        except (TypeError, ValueError):
            errors.append("修订号必须是不小于 1 的整数")
    item["revision_no"] = revision_no

    for field in ("accident_no", "accident_desc", "policy_no", "customer_name",
                  "incident_type", "region", "claimant_phone"):
        value = _pick(raw, field)
        item[field] = str(value).strip() if value is not None else ""

    incident_date = _parse_date(_pick(raw, "incident_date")) if _pick(raw, "incident_date") is not None else None
    if incident_date is None:
        errors.append("出险日期无效，应为 YYYY-MM-DD")
    item["incident_date"] = incident_date

    amount_value = _pick(raw, "loss_amount")
    loss_amount: Decimal | None = None
    try:
        loss_amount = Decimal(str(amount_value).replace(",", "").strip())
        if not loss_amount.is_finite() or loss_amount < 0:
            errors.append("损失金额必须是不小于 0 的数字")
            loss_amount = None
    except (InvalidOperation, AttributeError):
        errors.append("损失金额必须是不小于 0 的数字")
    item["loss_amount"] = loss_amount

    documents = _pick(raw, "required_documents")
    if documents is None:
        item["required_documents"] = list(DEFAULT_DOCUMENTS)
    elif isinstance(documents, list) and all(isinstance(d, str) and d.strip() for d in documents):
        item["required_documents"] = [d.strip() for d in documents]
    else:
        errors.append("所需材料必须是字符串数组")
        item["required_documents"] = list(DEFAULT_DOCUMENTS)

    for field in ("policy_no", "customer_name", "incident_type", "region", "claimant_phone"):
        if not item[field]:
            errors.append(f"缺少{FIELD_CN.get(field, field)}")

    # 事故标识允许为空：不同报案号本身就代表不同事故；accident_no 仅作补充。
    item["accident_no"] = item.get("accident_no") or None
    item["accident_desc"] = item.get("accident_desc") or None

    if errors:
        return None, errors

    item["content_hash"] = fingerprint(_content_projection(item))
    return item, []


def _content_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_no": item["policy_no"],
        "customer_name": item["customer_name"],
        "incident_type": item["incident_type"],
        "incident_date": item["incident_date"].isoformat() if item["incident_date"] else None,
        "loss_amount": str(item["loss_amount"]) if item["loss_amount"] is not None else None,
        "region": item["region"],
        "claimant_phone": item["claimant_phone"],
        "accident_no": item.get("accident_no"),
        "accident_desc": item.get("accident_desc"),
        "required_documents": item["required_documents"],
    }


def _is_graded(claim: dict[str, Any] | None) -> bool:
    if not claim:
        return False
    return claim.get("risk_level") is not None or claim.get("status") in GRADED_STATUSES


def _next_claim_identity(conn, incident_date: date) -> tuple[int, str]:
    # 只取一次序列：显式写入同一 id，claim_no 后缀与真实 id 保持一致且不跳号。
    next_id = conn.execute("SELECT nextval(pg_get_serial_sequence('claims','id')) AS id").fetchone()["id"]
    return next_id, f"CLM-{incident_date.strftime('%y%m%d')}-{next_id:04d}"


def _insert_claim(conn, item: dict[str, Any], batch_no: str) -> dict[str, Any]:
    claim_id, claim_no = _next_claim_identity(conn, item["incident_date"])
    required = item["required_documents"]
    return conn.execute(
        """INSERT INTO claims(
               id, claim_no, policy_no, customer_name, incident_type, incident_date, loss_amount,
               region, claimant_phone, required_documents, received_documents, missing_documents,
               documents_status, status, report_no, revision_no, accident_no, accident_desc, report_batch_no
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'missing','intake',%s,%s,%s,%s,%s)
           RETURNING *""",
        (
            claim_id, claim_no, item["policy_no"], item["customer_name"], item["incident_type"],
            item["incident_date"], item["loss_amount"], item["region"], item["claimant_phone"],
            json.dumps(required, ensure_ascii=False), "[]",
            json.dumps(required, ensure_ascii=False),
            item["report_no"], item["revision_no"], item.get("accident_no"),
            item.get("accident_desc"), batch_no,
        ),
    ).fetchone()


def _apply_item_to_claim(conn, claim_id: int, item: dict[str, Any], revision_no: int) -> dict[str, Any]:
    """把修订内容写到案件上；保留仍在新材料清单内的已收材料。"""
    claim = conn.execute("SELECT * FROM claims WHERE id=%s FOR UPDATE", (claim_id,)).fetchone()
    required = item["required_documents"]
    received = [d for d in (claim["received_documents"] or []) if d in required]
    missing = sorted(set(required) - set(received))
    documents_status = "complete" if not missing else "missing"
    return conn.execute(
        """UPDATE claims SET
               policy_no=%s, customer_name=%s, incident_type=%s, incident_date=%s,
               loss_amount=%s, region=%s, claimant_phone=%s, required_documents=%s,
               received_documents=%s, missing_documents=%s, documents_status=%s,
               revision_no=%s, accident_no=%s, accident_desc=%s, updated_at=now()
           WHERE id=%s RETURNING *""",
        (
            item["policy_no"], item["customer_name"], item["incident_type"], item["incident_date"],
            item["loss_amount"], item["region"], item["claimant_phone"],
            json.dumps(required, ensure_ascii=False), json.dumps(received, ensure_ascii=False),
            json.dumps(missing, ensure_ascii=False), documents_status, revision_no,
            item.get("accident_no"), item.get("accident_desc"), claim_id,
        ),
    ).fetchone()


def _item_result(index, report_no, revision_no, status, message,
                 claim_id=None, claim_no=None, content_hash=None, raw=None, errors=None):
    return {
        "item_index": index,
        "report_no": report_no or "",
        "revision_no": revision_no,
        "content_hash": content_hash,
        "status": status,
        "status_label": status_label(status),
        "message": message,
        "claim_id": claim_id,
        "claim_no": claim_no,
        "errors": errors or [],
        "payload": raw if isinstance(raw, dict) else None,
    }


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    keys = ("created", "updated", "duplicate", "stale_revision",
            "pending_confirmation", "conflict", "invalid", "not_processed")
    counts = {key: 0 for key in keys}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def _persist_batch(conn, *, batch_no, partner_code, status, request_hash, raw_payload,
                   receipt, user_id, item_results, reject_reason=None):
    counts = _counts(item_results)
    row = conn.execute(
        """INSERT INTO report_batches(
               batch_no, partner_code, status, total_count, created_count, updated_count,
               duplicate_count, stale_count, pending_count, request_hash,
               request_payload, receipt, reject_reason, created_by
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (
            batch_no, partner_code, status, len(item_results), counts["created"], counts["updated"],
            counts["duplicate"], counts["stale_revision"], counts["pending_confirmation"],
            request_hash, json.dumps(raw_payload, ensure_ascii=False, default=str),
            json.dumps(receipt, ensure_ascii=False, default=str), reject_reason, user_id,
        ),
    ).fetchone()
    batch_id = row["id"]
    for result in item_results:
        conn.execute(
            """INSERT INTO report_batch_items(
                   batch_id, item_index, report_no, revision_no, content_hash, payload,
                   status, message, claim_id, claim_no
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                batch_id, result["item_index"], result["report_no"],
                result["revision_no"] if isinstance(result["revision_no"], int) else 0,
                result.get("content_hash") or "",
                json.dumps(result.get("payload") or {}, ensure_ascii=False, default=str),
                result["status"], result["message"], result.get("claim_id"), result.get("claim_no"),
            ),
        )
    return batch_id


def submit_batch(batch, user_id: int) -> dict[str, Any]:
    """batch: src.schemas.batch_reports.ReportBatchSubmit。"""
    raw_payload = batch.model_dump(mode="json")
    request_hash = fingerprint({"partner_code": batch.partner_code, "items": batch.items})

    with get_conn() as conn:
        # 串行化批次受理，彻底消除同批次号/同报案号的 check-then-insert 竞争；
        # 与风险分级之间仍通过 claims 行锁保证并发正确性。
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BATCH_SERIAL_LOCK,))

        existing = None
        if batch.batch_no:
            existing = conn.execute(
                "SELECT * FROM report_batches WHERE batch_no=%s FOR UPDATE", (batch.batch_no,)
            ).fetchone()

        if existing and existing["request_hash"] == request_hash:
            receipt = dict(existing["receipt"])
            receipt["replayed"] = True
            receipt["http_note"] = "相同批次的重复请求，返回原回执，未重复立案"
            return receipt

        if existing and existing["status"] == "accepted":
            raise BatchNumberConflict("批次号已受理且报文内容不同，不能复用批次号提交不同内容")

        # 同批次号曾被退回：允许用修正后的整批内容覆盖该退回回执。
        if existing and existing["status"] == "rejected":
            conn.execute("DELETE FROM report_batch_items WHERE batch_id=%s", (existing["id"],))
            conn.execute("DELETE FROM report_batches WHERE id=%s", (existing["id"],))

        batch_no = batch.batch_no or conn.execute(
            "SELECT 'B'||to_char(now() AT TIME ZONE 'Asia/Shanghai','YYMMDD')||'-'||lpad(nextval('report_batch_no_seq')::text,4,'0') AS no"
        ).fetchone()["no"]

        # ---------- 第一阶段：整批校验（只做检查，不写案件） ----------
        normalized: list[dict[str, Any] | None] = []
        results: list[dict[str, Any]] = []
        fatal = False
        seen_keys: dict[tuple[str, int], int] = {}

        for index, raw in enumerate(batch.items):
            item, errors = normalize_item(raw)
            if errors:
                fatal = True
                normalized.append(None)
                report_no = str(raw.get("report_no") or raw.get("reportNo") or raw.get("报案号") or "") if isinstance(raw, dict) else ""
                results.append(_item_result(
                    index, report_no, None, "invalid", "；".join(errors),
                    raw=raw if isinstance(raw, dict) else None, errors=errors,
                ))
                continue

            normalized.append(item)
            key = (item["report_no"], item["revision_no"])
            prior_index = seen_keys.get(key)
            if prior_index is not None:
                first = normalized[prior_index]
                if first and first["content_hash"] != item["content_hash"]:
                    fatal = True
                    results.append(_item_result(
                        index, item["report_no"], item["revision_no"], "conflict",
                        f"与本批第 {prior_index + 1} 条报案号和修订号相同但内容不同，整批退回",
                        content_hash=item["content_hash"], raw=raw,
                    ))
                else:
                    results.append(_item_result(
                        index, item["report_no"], item["revision_no"], "duplicate",
                        f"与本批第 {prior_index + 1} 条内容完全相同，按重复报送处理",
                        content_hash=item["content_hash"], raw=raw,
                    ))
                continue

            seen_keys[key] = index
            stored = conn.execute(
                "SELECT rv.*, r.claim_id AS report_claim_id FROM report_revisions rv "
                "LEFT JOIN partner_reports r ON r.report_no=rv.report_no "
                "WHERE rv.report_no=%s AND rv.revision_no=%s",
                key,
            ).fetchone()
            if stored and stored["content_hash"] != item["content_hash"]:
                fatal = True
                results.append(_item_result(
                    index, item["report_no"], item["revision_no"], "conflict",
                    "报案号和修订号与已接收资料相同但内容不同，整批退回；内容修正请使用新的修订号",
                    claim_id=stored["claim_id"], content_hash=item["content_hash"], raw=raw,
                ))
            else:
                # 占位，第二阶段给出最终处置。
                results.append(_item_result(
                    index, item["report_no"], item["revision_no"], "not_processed", "待受理",
                    content_hash=item["content_hash"], raw=raw,
                ))

        if fatal:
            reason = "整批校验未通过：存在字段错误或同报案号同修订号内容冲突，本批全部未受理"
            receipt = {
                "batch_no": batch_no,
                "partner_code": batch.partner_code,
                "status": "rejected",
                "replayed": False,
                "total": len(batch.items),
                "counts": _counts(results),
                "reject_reason": reason,
                "created_at": None,
                "items": [
                    {k: v for k, v in r.items() if k != "payload"} for r in results
                ],
            }
            batch_id = _persist_batch(
                conn, batch_no=batch_no, partner_code=batch.partner_code, status="rejected",
                request_hash=request_hash, raw_payload=raw_payload, receipt=receipt,
                user_id=user_id, item_results=results, reject_reason=reason,
            )
            receipt["id"] = batch_id
            record_in_transaction(conn, user_id, "report_batch_rejected", None, {
                "batch_no": batch_no, "counts": receipt["counts"],
            })
            return receipt

        # ---------- 第二阶段：整批有效，逐条目处理（同一事务） ----------
        key_results: dict[tuple[str, int], dict[str, Any]] = {}
        final_results: list[dict[str, Any]] = []

        for index, item in enumerate(normalized):
            # 字段非法时第一阶段已 fatal，这里 normalized 必然非空。
            key = (item["report_no"], item["revision_no"])
            preliminary = results[index]
            if preliminary["status"] == "duplicate":
                source = key_results.get(key)
                final_results.append(_item_result(
                    index, item["report_no"], item["revision_no"], "duplicate",
                    preliminary["message"],
                    claim_id=source["claim_id"] if source else None,
                    claim_no=source["claim_no"] if source else None,
                    content_hash=item["content_hash"], raw=batch.items[index],
                ))
                continue

            result = _process_valid_item(conn, item, batch_no, user_id)
            result["item_index"] = index
            result["payload"] = batch.items[index]
            key_results[key] = result
            final_results.append(result)

        now = conn.execute("SELECT to_char(now(),'YYYY-MM-DD\"T\"HH24:MI:SSOF') AS iso").fetchone()["iso"]
        receipt = {
            "batch_no": batch_no,
            "partner_code": batch.partner_code,
            "status": "accepted",
            "replayed": False,
            "total": len(batch.items),
            "counts": _counts(final_results),
            "reject_reason": None,
            "created_at": now,
            "items": [
                {k: v for k, v in r.items() if k != "payload"} for r in final_results
            ],
        }
        batch_id = _persist_batch(
            conn, batch_no=batch_no, partner_code=batch.partner_code, status="accepted",
            request_hash=request_hash, raw_payload=raw_payload, receipt=receipt,
            user_id=user_id, item_results=final_results,
        )
        receipt["id"] = batch_id
        record_in_transaction(conn, user_id, "report_batch_accepted", None, {
            "batch_no": batch_no, "counts": receipt["counts"],
        })
        return receipt


def _process_valid_item(conn, item: dict[str, Any], batch_no: str, user_id: int) -> dict[str, Any]:
    report_no = item["report_no"]
    revision_no = item["revision_no"]

    existing_rev = conn.execute(
        "SELECT * FROM report_revisions WHERE report_no=%s AND revision_no=%s FOR UPDATE",
        (report_no, revision_no),
    ).fetchone()
    if existing_rev:
        claim_no = conn.execute("SELECT claim_no FROM claims WHERE id=%s",
                                (existing_rev["claim_id"],)).fetchone()
        return _item_result(
            -1, report_no, revision_no, "duplicate",
            "该报案号与修订号已接收且内容一致，按重复报送处理，不重复立案",
            claim_id=existing_rev["claim_id"],
            claim_no=claim_no["claim_no"] if claim_no else None,
            content_hash=item["content_hash"],
        )

    report = conn.execute(
        "SELECT * FROM partner_reports WHERE report_no=%s FOR UPDATE", (report_no,)
    ).fetchone()

    if report is None:
        # 新报案：报案号本身区分事故，同保单同日期的不同报案号分别建案。
        claim = _insert_claim(conn, item, batch_no)
        conn.execute(
            """INSERT INTO partner_reports(report_no, claim_id, current_revision, policy_no, incident_date, accident_no)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (report_no, claim["id"], revision_no, item["policy_no"],
             item["incident_date"], item.get("accident_no")),
        )
        conn.execute(
            """INSERT INTO report_revisions(report_no, revision_no, content_hash, payload, status, claim_id, batch_no)
               VALUES (%s,%s,%s,%s,'applied',%s,%s)""",
            (report_no, revision_no, item["content_hash"],
             json.dumps(_content_projection(item), ensure_ascii=False), claim["id"], batch_no),
        )
        record_in_transaction(conn, user_id, "claim_created", claim["id"], {
            "claim_no": claim["claim_no"], "report_no": report_no,
            "revision_no": revision_no, "batch_no": batch_no, "source": "report_batch",
        })
        return _item_result(
            -1, report_no, revision_no, "created", "校验通过，已单独立案",
            claim_id=claim["id"], claim_no=claim["claim_no"], content_hash=item["content_hash"],
        )

    if revision_no < report["current_revision"]:
        claim_no = conn.execute("SELECT claim_no FROM claims WHERE id=%s",
                                (report["claim_id"],)).fetchone()
        return _item_result(
            -1, report_no, revision_no, "stale_revision",
            f"乱序到达的旧修订（当前已有第 {report['current_revision']} 版），不覆盖现有资料",
            claim_id=report["claim_id"],
            claim_no=claim_no["claim_no"] if claim_no else None,
            content_hash=item["content_hash"],
        )

    # 对 claims 行加锁并在锁内复检分级状态；与 model_claim 的 FOR UPDATE 互斥，
    # 因此“检查时未分级、随后被分级”的并发窗口不存在。
    claim = conn.execute(
        "SELECT * FROM claims WHERE id=%s FOR UPDATE", (report["claim_id"],)
    ).fetchone() if report["claim_id"] else None

    if _is_graded(claim):
        revision_row = conn.execute(
            """INSERT INTO report_revisions(report_no, revision_no, content_hash, payload, status, claim_id, batch_no)
               VALUES (%s,%s,%s,%s,'pending',%s,%s) RETURNING id""",
            (report_no, revision_no, item["content_hash"],
             json.dumps(_content_projection(item), ensure_ascii=False), claim["id"], batch_no),
        ).fetchone()
        record_in_transaction(conn, user_id, "report_revision_pending", claim["id"], {
            "report_no": report_no, "revision_no": revision_no,
            "revision_id": revision_row["id"], "batch_no": batch_no,
            "reason": "案件已完成风险分级，新修订留待人工确认",
        })
        return _item_result(
            -1, report_no, revision_no, "pending_confirmation",
            "案件已分级，新修订未覆盖原资料，已转人工确认",
            claim_id=claim["id"], claim_no=claim["claim_no"],
            content_hash=item["content_hash"],
        )

    updated = _apply_item_to_claim(conn, report["claim_id"], item, revision_no)
    conn.execute(
        """INSERT INTO report_revisions(report_no, revision_no, content_hash, payload, status, claim_id, batch_no)
           VALUES (%s,%s,%s,%s,'applied',%s,%s)""",
        (report_no, revision_no, item["content_hash"],
         json.dumps(_content_projection(item), ensure_ascii=False), report["claim_id"], batch_no),
    )
    conn.execute(
        """UPDATE partner_reports
           SET current_revision=%s, policy_no=%s, incident_date=%s, accident_no=%s, updated_at=now()
           WHERE id=%s""",
        (revision_no, item["policy_no"], item["incident_date"],
         item.get("accident_no"), report["id"]),
    )
    record_in_transaction(conn, user_id, "claim_updated_by_revision", report["claim_id"], {
        "claim_no": updated["claim_no"], "report_no": report_no,
        "revision_no": revision_no, "batch_no": batch_no,
    })
    return _item_result(
        -1, report_no, revision_no, "updated", "新修订已更新案件资料",
        claim_id=report["claim_id"], claim_no=updated["claim_no"],
        content_hash=item["content_hash"],
    )


def _serialize_batch(row: dict[str, Any], include_payload: bool = False) -> dict[str, Any]:
    data = dict(row)
    if isinstance(data.get("receipt"), str):
        data["receipt"] = json.loads(data["receipt"])
    if isinstance(data.get("request_payload"), str):
        data["request_payload"] = json.loads(data["request_payload"])
    if not include_payload:
        data.pop("request_payload", None)
    created = data.get("created_at")
    if isinstance(created, date):
        data["created_at"] = created.isoformat()
    return data


def get_batch(batch_no: str) -> dict[str, Any]:
    with get_conn() as conn:
        batch = conn.execute("SELECT * FROM report_batches WHERE batch_no=%s", (batch_no,)).fetchone()
        if not batch:
            raise LookupError("批次回执不存在")
        items = conn.execute(
            "SELECT * FROM report_batch_items WHERE batch_id=%s ORDER BY item_index", (batch["id"],)
        ).fetchall()
    result = _serialize_batch(batch, include_payload=True)
    result["items"] = []
    for item in items:
        entry = dict(item)
        if isinstance(entry.get("payload"), str):
            entry["payload"] = json.loads(entry["payload"])
        created = entry.get("created_at")
        if isinstance(created, date):
            entry["created_at"] = created.isoformat()
        entry["status_label"] = status_label(entry["status"])
        result["items"].append(entry)
    # 以 receipt 快照为“原回执”，明细表为逐条结果；两者合并返回。
    result["original_receipt"] = result.get("receipt")
    return result


def list_batches(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT batch_no, partner_code, status, total_count, created_count, updated_count,
                      duplicate_count, stale_count, pending_count, reject_reason, created_by, created_at
               FROM report_batches ORDER BY id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        entry = dict(row)
        if isinstance(entry.get("created_at"), date):
            entry["created_at"] = entry["created_at"].isoformat()
        entry["status_label"] = status_label(entry["status"])
        items.append(entry)
    return {"items": items}


def list_claim_batches(claim_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        claim = conn.execute("SELECT id FROM claims WHERE id=%s", (claim_id,)).fetchone()
        if not claim:
            raise LookupError("案件不存在")
        rows = conn.execute(
            """SELECT DISTINCT b.batch_no, b.status, b.reject_reason, b.created_at,
                       (SELECT count(*) FROM report_batch_items i2
                        WHERE i2.batch_id=b.id AND i2.claim_id=%s) AS claim_item_count
                FROM report_batch_items i
                JOIN report_batches b ON b.id=i.batch_id
                WHERE i.claim_id=%s
                ORDER BY b.created_at DESC""",
            (claim_id, claim_id),
        ).fetchall()
    items = []
    for row in rows:
        entry = dict(row)
        if isinstance(entry.get("created_at"), date):
            entry["created_at"] = entry["created_at"].isoformat()
        entry["status_label"] = status_label(entry["status"])
        items.append(entry)
    return {"items": items}


def get_partner_report(report_no: str) -> dict[str, Any]:
    with get_conn() as conn:
        report = conn.execute("SELECT * FROM partner_reports WHERE report_no=%s", (report_no,)).fetchone()
        if not report:
            raise LookupError("报案号不存在")
        revisions = conn.execute(
            """SELECT rv.*, u.display_name AS reviewed_by_name
               FROM report_revisions rv LEFT JOIN users u ON u.id=rv.reviewed_by
               WHERE rv.report_no=%s ORDER BY rv.revision_no DESC""",
            (report_no,),
        ).fetchall()
    result = dict(report)
    if isinstance(result.get("created_at"), date):
        result["created_at"] = result["created_at"].isoformat()
        result["updated_at"] = result["updated_at"].isoformat()
    rev_items = []
    for rev in revisions:
        entry = dict(rev)
        if isinstance(entry.get("payload"), str):
            entry["payload"] = json.loads(entry["payload"])
        for key in ("created_at", "updated_at", "reviewed_at"):
            if isinstance(entry.get(key), date):
                entry[key] = entry[key].isoformat()
        entry["status_label"] = status_label(entry["status"])
        rev_items.append(entry)
    result["revisions"] = rev_items
    return result


def list_pending_revisions(claim_id: int | None = None) -> dict[str, Any]:
    sql = """
        SELECT rv.id AS revision_id, rv.report_no, rv.revision_no, rv.payload AS proposed_payload,
               rv.batch_no, rv.created_at AS proposed_at, rv.claim_id,
               c.claim_no, c.status AS claim_status, c.risk_level, c.policy_no, c.incident_date,
               c.customer_name, c.loss_amount, c.claimant_phone,
               c.revision_no AS current_claim_revision,
               r.current_revision
        FROM report_revisions rv
        JOIN claims c ON c.id = rv.claim_id
        JOIN partner_reports r ON r.report_no = rv.report_no
        WHERE rv.status='pending'
    """
    params: tuple[Any, ...] = ()
    if claim_id:
        sql += " AND rv.claim_id=%s"
        params = (claim_id,)
    sql += " ORDER BY rv.created_at ASC, rv.id ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = []
    for row in rows:
        entry = dict(row)
        if isinstance(entry.get("proposed_payload"), str):
            entry["proposed_payload"] = json.loads(entry["proposed_payload"])
        if isinstance(entry.get("proposed_at"), date):
            entry["proposed_at"] = entry["proposed_at"].isoformat()
        entry["status_label"] = status_label("pending")
        items.append(entry)
    return {"items": items, "count": len(items)}


def confirm_revision(revision_id: int, user_id: int, note: str | None = None,
                     regrade: bool = False) -> dict[str, Any]:
    with get_conn() as conn:
        rev = conn.execute(
            "SELECT * FROM report_revisions WHERE id=%s FOR UPDATE", (revision_id,)
        ).fetchone()
        if not rev:
            raise LookupError("修订记录不存在")
        if rev["status"] != "pending":
            raise PermissionError("该修订已完成人工确认，不能重复处理")

        report = conn.execute(
            "SELECT * FROM partner_reports WHERE report_no=%s FOR UPDATE", (rev["report_no"],)
        ).fetchone()
        claim = conn.execute(
            "SELECT * FROM claims WHERE id=%s FOR UPDATE", (rev["claim_id"],)
        ).fetchone()
        if not claim:
            raise LookupError("关联案件不存在")

        payload = rev["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        item = {
            "policy_no": payload.get("policy_no"),
            "customer_name": payload.get("customer_name"),
            "incident_type": payload.get("incident_type"),
            "incident_date": date.fromisoformat(payload["incident_date"]) if payload.get("incident_date") else None,
            "loss_amount": Decimal(str(payload.get("loss_amount"))),
            "region": payload.get("region"),
            "claimant_phone": payload.get("claimant_phone"),
            "required_documents": payload.get("required_documents") or list(DEFAULT_DOCUMENTS),
            "accident_no": payload.get("accident_no"),
            "accident_desc": payload.get("accident_desc"),
        }

        if rev["revision_no"] < report["current_revision"]:
            conn.execute(
                "UPDATE report_revisions SET status='superseded', reviewed_by=%s, reviewed_at=now(), review_note=%s, updated_at=now() WHERE id=%s",
                (user_id, note or "已有更新修订生效，本修订自动失效", revision_id),
            )
            record_in_transaction(conn, user_id, "report_revision_superseded", claim["id"], {
                "report_no": rev["report_no"], "revision_no": rev["revision_no"],
            })
            return {"id": revision_id, "status": "superseded", "status_label": status_label("superseded")}

        updated = _apply_item_to_claim(conn, claim["id"], item, rev["revision_no"])
        conn.execute(
            """UPDATE report_revisions
               SET status='confirmed', reviewed_by=%s, reviewed_at=now(), review_note=%s, updated_at=now()
               WHERE id=%s""",
            (user_id, note, revision_id),
        )
        conn.execute(
            "UPDATE partner_reports SET current_revision=%s, updated_at=now() WHERE id=%s",
            (rev["revision_no"], report["id"]),
        )
        # 更低版本的待确认修订已被本次内容覆盖，自动作废。
        conn.execute(
            """UPDATE report_revisions SET status='superseded', updated_at=now()
               WHERE report_no=%s AND status='pending' AND revision_no < %s""",
            (rev["report_no"], rev["revision_no"]),
        )
        regrade_requested = False
        if regrade:
            # 人工采纳新资料后要求重新分级：清空旧分级，退回待模型状态。
            conn.execute(
                """UPDATE claims SET risk_level=NULL, model_score=NULL, model_threshold=NULL,
                       status=CASE WHEN documents_status='complete' THEN 'initial_review' ELSE status END,
                       updated_at=now()
                   WHERE id=%s""",
                (claim["id"],),
            )
            regrade_requested = True
        record_in_transaction(conn, user_id, "report_revision_confirmed", claim["id"], {
            "report_no": rev["report_no"], "revision_no": rev["revision_no"],
            "revision_id": revision_id, "note": note, "regrade": regrade_requested,
        })
    return {
        "id": revision_id, "status": "confirmed", "status_label": status_label("confirmed"),
        "claim_id": claim["id"], "claim_no": updated["claim_no"], "regrade": regrade_requested,
    }


def reject_revision(revision_id: int, user_id: int, note: str | None = None) -> dict[str, Any]:
    with get_conn() as conn:
        rev = conn.execute(
            "SELECT * FROM report_revisions WHERE id=%s FOR UPDATE", (revision_id,)
        ).fetchone()
        if not rev:
            raise LookupError("修订记录不存在")
        if rev["status"] != "pending":
            raise PermissionError("该修订已完成人工确认，不能重复处理")
        conn.execute(
            """UPDATE report_revisions
               SET status='rejected', reviewed_by=%s, reviewed_at=now(),
                   review_note=COALESCE(%s, '人工确认后退回该修订'), updated_at=now()
               WHERE id=%s""",
            (user_id, note, revision_id),
        )
        record_in_transaction(conn, user_id, "report_revision_rejected", rev["claim_id"], {
            "report_no": rev["report_no"], "revision_no": rev["revision_no"],
            "revision_id": revision_id, "note": note,
        })
    return {"id": revision_id, "status": "rejected", "status_label": status_label("rejected")}
