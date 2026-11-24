import json
from datetime import date, timedelta
from decimal import Decimal
from src.utils.db import get_conn
from src.utils.auth import password_hash
from src.schemas.batch_reports import ReportBatchSubmit
from src.services.batch_report_service import submit_batch
from src.services.claim_service import verify_documents, model_claim

users = [
    ("intake@claims.local", "intake_agent", "周宁"),
    ("investigator@claims.local", "investigator", "陈默"),
    ("manager@claims.local", "risk_manager", "林岚"),
]
claims = [
    ("CLM-261101-0001", "PA-2026-0018", "赵海宁", "车损", date(2026,11,1), Decimal("86000"), "都市", "13800001111", ["身份证明","事故证明","损失清单"], ["身份证明","事故证明","损失清单"], [], "complete", "initial_review", None, False),
    ("CLM-261102-0002", "PA-2026-0027", "吴晓彤", "盗抢", date(2026,11,2), Decimal("168000"), "沿海", "13800002222", ["身份证明","事故证明","损失清单"], ["身份证明","事故证明"], ["损失清单"], "missing", "intake", None, True),
    ("CLM-261103-0003", "PA-2026-0031", "孙立诚", "水灾", date(2026,11,3), Decimal("236000"), "沿海", "13800003333", ["身份证明","事故证明","损失清单"], ["身份证明","事故证明","损失清单"], [], "complete", "initial_review", None, True),
    ("CLM-261104-0004", "PA-2026-0045", "周婉清", "医疗", date(2026,11,4), Decimal("42000"), "山区", "13800004444", ["身份证明","事故证明","损失清单"], ["身份证明","事故证明","损失清单"], [], "complete", "modeled", 0.61, False),
    ("CLM-261105-0005", "PA-2026-0052", "何志远", "火灾", date(2026,11,5), Decimal("510000"), "园区", "13800005555", ["身份证明","事故证明","损失清单"], ["身份证明","事故证明","损失清单"], [], "complete", "investigating", 0.86, True),
]

with get_conn() as conn:
    conn.execute("SELECT pg_advisory_xact_lock(1045003)")
    for username, role, display in users:
        conn.execute("INSERT INTO users(username,password_hash,role,display_name) VALUES (%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING", (username, password_hash("demo123"), role, display))
    settings = [("model_threshold", "0.62"), ("last_good_threshold", "0.62"), ("max_false_positive_rate", "0.25")]
    for key, value in settings:
        conn.execute("INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO NOTHING", (key, value))
    for item in claims:
        claim_no, policy_no, customer, incident, incident_date, loss, region, phone, required, received, missing, doc_status, status, score, fraud = item
        query = """INSERT INTO claims(claim_no,policy_no,customer_name,incident_type,incident_date,loss_amount,region,claimant_phone,required_documents,received_documents,missing_documents,documents_status,status,model_score,model_threshold,risk_level,verified_fraud)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0.62,%s,%s)
        ON CONFLICT(claim_no) DO NOTHING RETURNING id"""
        params = (claim_no, policy_no, customer, incident, incident_date, loss, region, phone, json.dumps(required, ensure_ascii=False), json.dumps(received, ensure_ascii=False), json.dumps(missing, ensure_ascii=False), doc_status, status, score, "high" if score and score >= 0.62 else "medium" if score else None, fraud)
        row = conn.execute(query, params).fetchone()
        if row is None:
            continue
        claim_id = row["id"]
        if claim_no == "CLM-261105-0005":
            conn.execute("INSERT INTO risk_clues(claim_id,clue_type,severity,description,evidence,status,created_by) SELECT %s,'出险时间异常','high','报案时间与气象记录存在偏差','气象站 WX-261105-07，降雨峰值晚于报案时间 3 小时','open',id FROM users WHERE username='manager@claims.local' AND NOT EXISTS (SELECT 1 FROM risk_clues WHERE claim_id=%s AND clue_type='出险时间异常')", (claim_id, claim_id))
            conn.execute("INSERT INTO investigation_tasks(claim_id,assignee,priority,status,due_date,scope) SELECT %s,'陈默','urgent','assigned',%s,'核对现场照片 EXIF、维修厂报价单与气象站记录' WHERE NOT EXISTS (SELECT 1 FROM investigation_tasks WHERE claim_id=%s)", (claim_id, date.today()+timedelta(days=2), claim_id))
        if claim_no == "CLM-261104-0004":
            conn.execute("INSERT INTO risk_clues(claim_id,clue_type,severity,description,evidence,status,created_by) SELECT %s,'历史频繁出险','medium','近 12 个月同类报案 3 次','保单 PA-2026-0045 历史赔案索引','open',id FROM users WHERE username='manager@claims.local' AND NOT EXISTS (SELECT 1 FROM risk_clues WHERE claim_id=%s AND clue_type='历史频繁出险')", (claim_id, claim_id))

# ---- 合作方批次报案演示（固定批次号 + 相同报文，重复执行走原回执，不重复立案） ----
with get_conn() as conn:
    intake_row = conn.execute("SELECT id FROM users WHERE username='intake@claims.local'").fetchone()
    manager_row = conn.execute("SELECT id FROM users WHERE username='manager@claims.local'").fetchone()
intake_id = intake_row["id"]
manager_id = manager_row["id"]

demo_batch = ReportBatchSubmit(batch_no="B-DEMO-261120-01", partner_code="HUADONG-DEMO", items=[
    {"report_no": "RPT-DEMO-261120-01", "revision_no": 1, "accident_no": "ACC-DEMO-A",
     "policy_no": "PA-2026-B88", "customer_name": "周宁远（追尾）", "incident_type": "车损",
     "incident_date": date(2026, 11, 20), "loss_amount": Decimal("12000"), "region": "都市",
     "claimant_phone": "13800008801"},
    {"report_no": "RPT-DEMO-261120-02", "revision_no": 1, "accident_no": "ACC-DEMO-B",
     "policy_no": "PA-2026-B88", "customer_name": "吴岚（变道剐蹭）", "incident_type": "车损",
     "incident_date": date(2026, 11, 20), "loss_amount": Decimal("6800"), "region": "都市",
     "claimant_phone": "13800008802"},
])
receipt = submit_batch(demo_batch, intake_id)
print(f"demo batch B-DEMO-261120-01: {receipt['status']} {receipt.get('counts')} replayed={receipt.get('replayed', False)}")

# 已分级案件 + 新修订 → 人工确认队列的演示（各步骤本身幂等）。
graded_batch = ReportBatchSubmit(batch_no="B-DEMO-261121-02", partner_code="HUADONG-DEMO", items=[
    {"report_no": "RPT-DEMO-GRADED-01", "revision_no": 1,
     "policy_no": "PA-2026-G77", "customer_name": "郑鹤", "incident_type": "水灾",
     "incident_date": date(2026, 11, 21), "loss_amount": Decimal("45000"), "region": "沿海",
     "claimant_phone": "13800007710"},
])
graded_receipt = submit_batch(graded_batch, intake_id)
demo_claim_id = graded_receipt["items"][0]["claim_id"]
verify_documents(demo_claim_id, ["身份证明", "事故证明", "损失清单"], intake_id)
model_claim(demo_claim_id, manager_id)
revision_batch = ReportBatchSubmit(batch_no="B-DEMO-261121-03", partner_code="HUADONG-DEMO", items=[
    {"report_no": "RPT-DEMO-GRADED-01", "revision_no": 2,
     "policy_no": "PA-2026-G77", "customer_name": "郑鹤（信息更正）", "incident_type": "水灾",
     "incident_date": date(2026, 11, 21), "loss_amount": Decimal("52800"), "region": "沿海",
     "claimant_phone": "13800007711"},
])
revision_receipt = submit_batch(revision_batch, intake_id)
print(f"demo graded revision B-DEMO-261121-03: {revision_receipt['items'][0]['status']} replayed={revision_receipt.get('replayed', False)}")
print('seed_demo completed idempotently')
