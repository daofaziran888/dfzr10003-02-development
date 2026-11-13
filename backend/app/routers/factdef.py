"""事实表管理接口：维度版本引用、发布阻断校验与同自然键冲突裁决。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.factdef import FactdefService

router = APIRouter(prefix="/api/factdef", tags=["事实表管理"])

service = FactdefService()

LIST_FIELDS = ["事实编号", "事实名称", "源表名称", "粒度说明", "关联维度", "度量字段", "分区字段", "最近刷新"]
STATUSES = ["草稿", "待审批", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按事实编号检索"),
    status: str | None = Query(default=None),
    target: str | None = Query(default=None, description="裁决/生效判断的目标日期，默认今天"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """事实清单：每行同步关联维度实际选中的版本与统一失效结论。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, target=target, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/conflicts", response_model=dict)
def arbitration(target: str | None = Query(default=None, description="裁决目标日期，默认今天")) -> dict[str, Any]:
    """同自然键不同版本引用的优先级裁决报告。"""
    return service.arbitration_report(target)


@router.get("/export")
def export_entries(target: str | None = None) -> dict[str, Any]:
    """导出事实表清单（含维度版本引用结论）。"""
    items, total = service.list_entries(page=1, size=10000, target=target)
    return {"module": "factdef", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int, target: str | None = None) -> dict:
    """读取单条事实定义及其维度版本引用结论。"""
    entry = service.get_entry(entry_id, target)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"事实定义 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/publish-check", response_model=dict)
def publish_check(entry_id: int, target: str | None = None) -> dict[str, Any]:
    """发布前校验：未生效版本或区间冲突都会给出阻断原因。"""
    return service.publish_check(entry_id, target)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条事实定义。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="事实定义已登记", entry=entry)


@router.put("/{entry_id}/references", response_model=ActionResult)
def set_reference(entry_id: int, payload: EntryPayload) -> ActionResult:
    """锁定事实引用的维度版本（维度编号 + 版本号）。"""
    dim_code = str(payload.values.get("维度编号") or "").strip()
    try:
        version_no = int(payload.values.get("version_no"))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="version_no 必须是整数版本号")
    entry, message = service.set_reference(entry_id, dim_code, version_no)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交审批/发布事实/废弃事实；发布时未生效或区间冲突的维度引用会阻断。"""
    action = str(payload.values.get("action") or "").strip()
    target = payload.values.get("target")
    entry, message = service.run_action(entry_id, action, target)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
