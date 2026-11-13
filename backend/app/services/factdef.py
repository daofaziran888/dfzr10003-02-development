"""事实表业务规则：维度版本引用、发布前阻断校验与同自然键冲突裁决。

事实页展示的维度状态一律调用 versioning.resolve_ref 计算，与维度定义页共用同一套
版本/失效结论，禁止只更新维度列表而事实页口径滞后。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store
from app import versioning as v
from app.services.dimdef import MODULE as DIM_MODULE

MODULE = "factdef"
REQUIRED_FIELDS = ["事实编号", "事实名称", "源表名称"]
STATUS_ORDER = ["草稿", "待审批", "已生效", "已废弃"]


class FactdefService:
    # ---------- 列表 / 明细：同步维度版本结论 ----------
    def _decorate(self, fact: dict[str, Any], target_day: date) -> dict[str, Any]:
        """给事实行挂上每个关联维度实际选中的版本与统一结论。"""
        refs = []
        blocked = False
        for ref in fact.get("维度引用", []):
            resolved = v.resolve_ref(
                store.rows(DIM_MODULE), ref.get("维度编号"), ref.get("version_no"), target_day
            )
            refs.append(resolved)
            blocked = blocked or not resolved["ok"]
        fact = dict(fact)
        fact["维度版本状态"] = refs
        fact["引用可发布"] = not blocked
        fact["阻断原因"] = [item["reason"] for item in refs if not item["ok"]]
        # 关联维度字段保留旧口径，同时给出可读的版本标签。
        fact["关联维度版本"] = [
            f"{item['维度编号']} v{item['version_no']}（{item.get('version_status', item.get('conclusion'))}）"
            for item in refs
        ]
        return fact

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        target: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        target_day = v.parse_day(target) or date.today()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("事实编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        decorated = [self._decorate(row, target_day) for row in rows]
        total = len(decorated)
        start = max(page - 1, 0) * size
        return decorated[start:start + size], total

    def get_entry(self, entry_id: int, target: str | None = None) -> dict[str, Any] | None:
        fact = store.find(MODULE, entry_id)
        if fact is None:
            return None
        target_day = v.parse_day(target) or date.today()
        return self._decorate(fact, target_day)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        if any(row.get("事实编号") == str(values["事实编号"]).strip() for row in rows):
            return None, ["事实编号已存在，不能重复登记"]
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "事实编号": str(values["事实编号"]).strip(),
            "事实名称": str(values.get("事实名称") or "").strip(),
            "源表名称": values.get("源表名称", ""),
            "粒度说明": values.get("粒度说明", ""),
            "关联维度": values.get("关联维度", ""),
            "度量字段": values.get("度量字段", ""),
            "分区字段": values.get("分区字段", ""),
            "最近刷新": values.get("最近刷新", ""),
            "事实批次": values.get("事实批次", ""),
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "维度引用": list(values.get("维度引用", [])),
        }
        rows.append(entry)
        return entry, []

    # ---------- 维度引用：显式锁定具体版本 ----------
    def set_reference(
        self,
        entry_id: int,
        dim_code: str,
        version_no: int,
    ) -> tuple[dict[str, Any] | None, str]:
        fact = store.find(MODULE, entry_id)
        if fact is None:
            return None, f"事实定义 {entry_id} 不存在或已归档"
        header = v.find_header(store.rows(DIM_MODULE), dim_code)
        if header is None:
            return None, f"维度 {dim_code} 不存在，无法建立引用"
        version = v.find_version(header, version_no)
        if version is None:
            return None, f"维度 {dim_code} 不存在 v{version_no}"
        refs = fact.setdefault("维度引用", [])
        for ref in refs:
            if ref.get("维度编号") == dim_code:
                ref["version_no"] = version_no
                break
        else:
            refs.append({"维度编号": dim_code, "version_no": version_no})
        fact["关联维度"] = "、".join(f"{r['维度编号']} v{r['version_no']}" for r in refs)
        return fact, f"已锁定 {dim_code} v{version_no}（{version['status']}）"

    # ---------- 发布前阻断 ----------
    def publish_check(self, entry_id: int, target: str | None = None) -> dict[str, Any]:
        fact = store.find(MODULE, entry_id)
        target_day = v.parse_day(target) or date.today()
        if fact is None:
            return {"ok": False, "blocked": True, "reasons": [f"事实定义 {entry_id} 不存在或已归档"]}
        refs = fact.get("维度引用", [])
        if not refs:
            return {"ok": False, "blocked": True, "reasons": ["尚未关联任何维度版本，不能发布"], "refs": []}
        resolved = [
            v.resolve_ref(store.rows(DIM_MODULE), ref.get("维度编号"), ref.get("version_no"), target_day)
            for ref in refs
        ]
        reasons = [f"{item['维度编号']} v{item['version_no']}：{item['reason']}" for item in resolved if not item["ok"]]
        return {
            "ok": not reasons,
            "blocked": bool(reasons),
            "target_date": target_day.isoformat(),
            "reasons": reasons,
            "refs": resolved,
        }

    def run_action(self, entry_id: int, action: str, target: str | None = None) -> tuple[dict[str, Any] | None, str]:
        fact = store.find(MODULE, entry_id)
        if fact is None:
            return None, f"事实定义 {entry_id} 不存在或已归档"

        if action == "提交审批":
            if fact["status"] != "草稿":
                return None, f"事实当前为{fact['status']}，只有草稿能提交审批"
            fact["status"] = "待审批"
            fact["pending"] = True
            return fact, "事实定义已提交审批"

        if action == "发布事实":
            # 不能跳级：待审批才能发布。
            if fact["status"] != "待审批":
                return None, f"不能跳级发布：事实当前为{fact['status']}，需先提交审批"
            check = self.publish_check(entry_id, target)
            if check["blocked"]:
                return None, "发布被阻断：" + "；".join(check["reasons"])
            fact["status"] = "已生效"
            fact["pending"] = False
            return fact, "事实定义已发布，引用的维度版本已随事实留档"

        if action == "废弃事实":
            fact["status"] = "已废弃"
            fact["pending"] = False
            fact["abnormal"] = True
            return fact, "事实定义已废弃"

        return None, f"动作「{action}」不属于事实表管理可执行范围"

    # ---------- 同自然键冲突裁决 ----------
    def arbitration_report(self, target: str | None = None) -> dict[str, Any]:
        target_day = v.parse_day(target) or date.today()
        facts = store.rows(MODULE)
        reports = v.arbitrate(store.rows(DIM_MODULE), facts, target_day)
        return {"target_date": target_day.isoformat(), "conflicts": reports, "total": len(reports)}
