from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def first_present(data: dict, keys: tuple[str, ...]):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


class ReportBatchSubmit(BaseModel):
    """合作方按批报案请求。

    明细先按宽松字典接收，逐条业务校验在 service 完成，
    这样任一条目有问题时仍能产出可查询的整批退回回执。
    """

    model_config = ConfigDict(extra="ignore")

    batch_no: str | None = None
    partner_code: str | None = None
    items: list[dict[str, Any]] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if not data.get("batch_no"):
            value = first_present(data, ("batchNo", "batch_id", "batchId", "batch_number", "批次号", "批次编号"))
            if value is not None:
                data["batch_no"] = str(value).strip()
        if not data.get("partner_code"):
            value = first_present(data, ("partnerCode", "partner_id", "partner", "partner_name", "合作方编码", "合作方"))
            if value is not None:
                data["partner_code"] = str(value).strip()
        if not data.get("items"):
            for key in ("records", "reports", "entries", "claim_items", "claimItems", "list", "报案明细", "明细", "items"):
                if isinstance(data.get(key), list) and data[key]:
                    data["items"] = data[key]
                    break
        return data


class RevisionReview(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    regrade: bool = False
