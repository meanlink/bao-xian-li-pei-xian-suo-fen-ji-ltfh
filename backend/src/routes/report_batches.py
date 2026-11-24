from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from src.schemas.batch_reports import ReportBatchSubmit, RevisionReview
from src.services import batch_report_service as svc
from src.utils.auth import current_user, require_roles

router = APIRouter(tags=["report batches"])
intake_staff = require_roles("intake_agent", "risk_manager")


def _not_found_or_400(exc: Exception):
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


@router.post("/report-batches")
@router.post("/batches")
@router.post("/claims/batch")
@router.post("/claims/report-batches")
def submit_report_batch(payload: ReportBatchSubmit, user=Depends(intake_staff)):
    try:
        receipt = svc.submit_batch(payload, user["id"])
    except svc.BatchNumberConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if receipt.get("status") == "rejected":
        # 整批退回：409 表示与已接收资料冲突/校验未通过，body 仍带回完整逐条结果。
        body = dict(receipt)
        body["detail"] = receipt.get("reject_reason") or "整批退回"
        return JSONResponse(status_code=409, content=body)
    return receipt


@router.get("/report-batches")
@router.get("/batches")
def list_report_batches(limit: int = 50, user=Depends(current_user)):
    return svc.list_batches(limit)


@router.get("/report-batches/{batch_no}")
@router.get("/batches/{batch_no}")
@router.get("/claims/report-batches/{batch_no}")
def get_report_batch(batch_no: str, user=Depends(current_user)):
    try:
        return svc.get_batch(batch_no)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/claims/{claim_id}/report-batches")
def claim_report_batches(claim_id: int, user=Depends(current_user)):
    try:
        return svc.list_claim_batches(claim_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/partner-reports/pending-revisions")
@router.get("/report-revisions/pending")
@router.get("/revision-requests")
def pending_revisions(claim_id: int | None = None, user=Depends(current_user)):
    return svc.list_pending_revisions(claim_id)


@router.get("/partner-reports/{report_no}")
def partner_report(report_no: str, user=Depends(current_user)):
    try:
        return svc.get_partner_report(report_no)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/report-revisions/{revision_id}/confirm")
@router.post("/revision-requests/{revision_id}/confirm")
def confirm_revision(revision_id: int, payload: RevisionReview | None = None,
                     user=Depends(intake_staff)):
    payload = payload or RevisionReview()
    try:
        return svc.confirm_revision(revision_id, user["id"], payload.note, payload.regrade)
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_400(exc) from exc


@router.post("/report-revisions/{revision_id}/reject")
@router.post("/revision-requests/{revision_id}/reject")
def reject_revision(revision_id: int, payload: RevisionReview | None = None,
                    user=Depends(intake_staff)):
    payload = payload or RevisionReview()
    try:
        return svc.reject_revision(revision_id, user["id"], payload.note)
    except (LookupError, PermissionError, ValueError) as exc:
        raise _not_found_or_400(exc) from exc
