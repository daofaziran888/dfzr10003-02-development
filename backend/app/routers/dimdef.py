"""维度定义管理接口：维度头 + 版本链，覆盖版本编辑、回填、审批与时间线查看。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dimdef import DimdefService

router = APIRouter(prefix="/api/dimdef", tags=["维度定义管理"])

service = DimdefService()

LIST_FIELDS = ["维度编号", "维度名称", "源表名称", "自然键列", "代理键列", "缓慢变化策略", "生效日期", "失效日期"]
STATUSES = ["草稿", "待审批", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按维度编号检索"),
    status: str | None = Query(default=None, description="草稿、待审批、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """维度头列表：状态由版本链统一回算，带当前版本号与版本数量。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/todos", response_model=dict)
def list_todos() -> dict[str, Any]:
    """审批待办：版本粒度去重，同一版本重复提交只出现一份。"""
    items = service.list_todos()
    return {"total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出维度定义清单（含完整版本链）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dimdef", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条维度定义（含版本链）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"维度定义 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/timeline", response_model=dict)
def get_timeline(entry_id: int) -> dict:
    """版本链时间线：按版本号列出各版本状态、区间、完整度与操作履历。"""
    timeline = service.timeline(entry_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail=f"维度定义 {entry_id} 不存在或已归档")
    return timeline


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条维度定义，自动生成 v1 草稿。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="维度定义已登记，v1 草稿已生成", entry=entry)


@router.put("/{entry_id}/versions/{version_no}", response_model=ActionResult)
def edit_version(entry_id: int, version_no: int, payload: EntryPayload) -> ActionResult:
    """编辑版本：结构变更生成新版本且不覆盖在档版本，草稿就地更新。"""
    version, message, kind = service.edit_version(entry_id, version_no, payload.values)
    if version is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=version)


@router.post("/{entry_id}/versions/{version_no}/backfill", response_model=ActionResult)
def backfill_version(entry_id: int, version_no: int, payload: EntryPayload) -> ActionResult:
    """晚到修正：回填到指定在档版本所属的历史区间，独立出版本而不改写当前版本。"""
    version, message = service.backfill_version(entry_id, version_no, payload.values)
    if version is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=version)


@router.post("/{entry_id}/versions/{version_no}/actions", response_model=ActionResult)
def run_version_action(entry_id: int, version_no: int, payload: EntryPayload) -> ActionResult:
    """版本级动作：提交审批/发布维度/废弃版本，强制顺序推进、禁止跳级与覆盖。"""
    action = str(payload.values.get("action") or "").strip()
    version, message = service.run_version_action(entry_id, version_no, action)
    if version is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=version)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """兼容旧入口：动作作用于链尾版本（废弃维度作用于整条链）。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
