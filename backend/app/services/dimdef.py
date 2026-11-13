"""维度定义业务规则：版本链、状态推进、回填修正与审批待办去重。

与旧实现的关键区别：影响自然键/代理键/缓慢变化策略的修改不再覆盖原记录，而是在
版本链上生成新版本；已生效版本只读，晚到修正走回填；状态只能草稿→待审批→已生效
顺序推进，同一版本重复提交审批只保留一份待办。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store
from app import versioning as v

MODULE = "dimdef"
REQUIRED_FIELDS = ["维度编号", "维度名称", "源表名称"]
STATUS_ORDER = ["草稿", "待审批", "已生效", "已废弃"]

# 版本上允许编辑的全部业务字段。
VERSION_FIELDS = ("源表名称", *v.STRUCTURAL_FIELDS, "生效日期", "失效日期")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class DimdefService:
    # ---------- 列表 / 明细 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("维度编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = [v.refresh_header(dict(row)) for row in rows]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        header = store.find(MODULE, entry_id)
        return v.refresh_header(header) if header else None

    def get_by_code(self, dim_code: str) -> dict[str, Any] | None:
        header = v.find_header(store.rows(MODULE), dim_code)
        return v.refresh_header(header) if header else None

    # ---------- 维度头登记 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        if v.find_header(rows, str(values["维度编号"]).strip()):
            return None, ["维度编号已存在，不能重复登记"]
        now = _now()
        version = {
            "version_no": 1,
            "status": v.VERSION_DRAFT,
            "backfill_of": None,
            **{field: values.get(field, "") for field in VERSION_FIELDS},
            "change_note": str(values.get("change_note") or "初始登记"),
            "created_at": now,
            "history": [{"action": "创建版本", "at": now, "by": "维度管理员", "note": "初始登记 v1"}],
        }
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "维度编号": str(values["维度编号"]).strip(),
            "维度名称": str(values.get("维度名称") or "").strip(),
            "status": v.VERSION_DRAFT,
            "pending": False,
            "abnormal": False,
            "current_version_no": 1,
            "versions": [version],
        }
        rows.append(entry)
        return v.refresh_header(entry), []

    # ---------- 版本链查看 ----------
    def timeline(self, entry_id: int) -> dict[str, Any] | None:
        header = store.find(MODULE, entry_id)
        if header is None:
            return None
        v.refresh_header(header)
        versions = v.chain_versions(header)
        return {
            "维度编号": header["维度编号"],
            "维度名称": header.get("维度名称"),
            "current_version_no": header["current_version_no"],
            "status": header["status"],
            "versions": [
                {
                    **version,
                    "completeness": v.version_completeness(version),
                    "editable": version["status"] == v.VERSION_DRAFT,
                    "closed": version["status"] in v.VERSION_CLOSED,
                }
                for version in versions
            ],
        }

    def list_todos(self) -> list[dict[str, Any]]:
        """审批待办：以版本为粒度去重，同一版本重复提交只出现一次。"""
        todos: list[dict[str, Any]] = []
        for header in store.rows(MODULE):
            for version in v.chain_versions(header):
                if version["status"] == v.VERSION_PENDING:
                    todos.append({
                        "todo_key": f"{header['维度编号']}#v{version['version_no']}",
                        "维度编号": header["维度编号"],
                        "维度名称": header.get("维度名称"),
                        "version_no": version["version_no"],
                        "缓慢变化策略": version.get("缓慢变化策略"),
                        "生效日期": version.get("生效日期"),
                        "失效日期": version.get("失效日期"),
                        "change_note": version.get("change_note"),
                        "submitted_at": version["history"][-1]["at"] if version.get("history") else "",
                    })
        return todos

    # ---------- 版本编辑：结构变更生成新版本 ----------
    def edit_version(
        self,
        entry_id: int,
        version_no: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """编辑某个版本。

        返回 (版本或None, 消息, 动作类型)。动作类型为 updated / spawned / rejected。
        - 草稿版本：直接在原版本上修改（草稿可反复改，不产生留档碎片）；
        - 待审批/已生效/已失效/已归档版本上的结构字段修改：生成新的草稿版本，
          已生效版本保持原样，绝不覆盖；
        - 仅改生效/失效日期的在档版本修改：拒绝就地改写，提示走回填。
        """
        header = store.find(MODULE, entry_id)
        if header is None:
            return None, f"维度定义 {entry_id} 不存在或已归档", None
        target = v.find_version(header, version_no)
        if target is None:
            return None, f"版本 v{version_no} 不存在", None

        message = v.validate_interval(values)
        if message:
            return None, message, None

        now = _now()
        structural = v.structural_changed(target, values)
        interval_only = any(str(values.get(field) or "").strip() for field in v.INTERVAL_FIELDS) and not structural

        # 草稿与待审批版本尚未生效，没有留档记录会被破坏，允许就地修正（待办仍是同一份）。
        if target["status"] in (v.VERSION_DRAFT, v.VERSION_PENDING):
            for field in VERSION_FIELDS:
                if field in values and values[field] is not None:
                    target[field] = values[field]
            if values.get("change_note"):
                target["change_note"] = values["change_note"]
            target["history"].append({
                "action": "编辑草稿" if target["status"] == v.VERSION_DRAFT else "审批前修正",
                "at": now, "by": "维度管理员",
                "note": values.get("change_note") or f"{target['status']}版本内容更新",
            })
            v.refresh_header(header)
            return target, f"{'草稿' if target['status'] == v.VERSION_DRAFT else '待审批'} v{version_no} 已更新", "updated"

        # 已生效/已失效/已归档版本不可覆盖。
        if interval_only:
            return None, (
                f"v{version_no} 状态为{target['status']}，生效区间修正必须通过「回填修正」进入所属区间，"
                "不能就地改写在档版本"
            ), "rejected"
        if not structural:
            return None, (
                f"v{version_no} 状态为{target['status']}，在档版本只读；"
                "仅日期调整请使用回填，结构字段调整将生成新版本"
            ), "rejected"

        # 已存在链尾草稿则并入，避免同一轮修改堆叠多个草稿。
        tip = v.tip_version(header)
        if tip is not None and tip["status"] == v.VERSION_DRAFT:
            for field in VERSION_FIELDS:
                if field in values and values[field] is not None:
                    tip[field] = values[field]
            tip["history"].append({
                "action": "并入结构变更", "at": now, "by": "维度管理员",
                "note": f"基于 v{version_no} 的结构修改并入草稿 v{tip['version_no']}",
            })
            v.refresh_header(header)
            return tip, f"结构变更已并入草稿 v{tip['version_no']}，v{version_no} 保持原样", "spawned"

        new_no = v.next_version_no(header)
        new_version = {
            "version_no": new_no,
            "status": v.VERSION_DRAFT,
            "backfill_of": None,
            **{field: target.get(field, "") for field in VERSION_FIELDS},
        }
        for field in VERSION_FIELDS:
            if field in values and values[field] is not None:
                new_version[field] = values[field]
        new_version["change_note"] = str(values.get("change_note") or f"基于 v{version_no} 的结构变更")
        new_version["created_at"] = now
        new_version["history"] = [{
            "action": "创建版本", "at": now, "by": "维度管理员",
            "note": f"自然键/代理键/策略变更，由 v{version_no} 派生 v{new_no}，原版本留档",
        }]
        header["versions"].append(new_version)
        v.refresh_header(header)
        return new_version, f"结构变更已生成草稿 v{new_no}，v{version_no} 未被覆盖", "spawned"

    # ---------- 回填：晚到修正进入历史区间 ----------
    def backfill_version(
        self,
        entry_id: int,
        base_version_no: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        header = store.find(MODULE, entry_id)
        if header is None:
            return None, f"维度定义 {entry_id} 不存在或已归档"
        base = v.find_version(header, base_version_no)
        if base is None:
            return None, f"被回填的基准版本 v{base_version_no} 不存在"
        if base["status"] not in (v.VERSION_ACTIVE, v.VERSION_EXPIRED):
            return None, f"只有在档（已生效/已失效）版本才能被回填，v{base_version_no} 当前为{base['status']}"

        message = v.validate_interval(values)
        if message:
            return None, message
        # 回填区间必须落在基准版本的历史区间内。
        backfill_start = v.parse_day(values.get("生效日期")) or v.parse_day(base.get("生效日期"))
        backfill_end = v.parse_day(values.get("失效日期")) or v.parse_day(base.get("失效日期"))
        base_start, base_end = v.parse_day(base.get("生效日期")), v.parse_day(base.get("失效日期"))
        if base_start and backfill_start and backfill_start < base_start:
            return None, "回填生效日期不能早于基准版本的生效日期"
        if base_end and backfill_end and backfill_end > base_end:
            return None, "回填失效日期不能晚于基准版本的失效日期（修正不能越过所属历史区间）"

        now = _now()
        new_no = v.next_version_no(header)
        backfill = {
            "version_no": new_no,
            "status": v.VERSION_DRAFT,
            "backfill_of": base_version_no,
            **{field: base.get(field, "") for field in VERSION_FIELDS},
        }
        for field in VERSION_FIELDS:
            if field in values and values[field] is not None:
                backfill[field] = values[field]
        backfill["change_note"] = str(values.get("change_note") or f"回填修正 v{base_version_no} 的历史区间")
        backfill["created_at"] = now
        backfill["history"] = [{
            "action": "创建回填版本", "at": now, "by": "维度管理员",
            "note": f"晚到修正回填 v{base_version_no} 所属区间，独立成 v{new_no}，不改写当前版本",
        }]
        header["versions"].append(backfill)
        v.refresh_header(header)
        return backfill, f"已生成回填草稿 v{new_no}，提交审批并发布后将在所属区间内优先于 v{base_version_no}"

    # ---------- 状态推进 ----------
    def run_version_action(
        self,
        entry_id: int,
        version_no: int,
        action: str,
    ) -> tuple[dict[str, Any] | None, str]:
        header = store.find(MODULE, entry_id)
        if header is None:
            return None, f"维度定义 {entry_id} 不存在或已归档"
        version = v.find_version(header, version_no)
        if version is None:
            return None, f"版本 v{version_no} 不存在"
        now = _now()

        if action == "提交审批":
            if version["status"] != v.VERSION_DRAFT:
                # 待审批再次提交：幂等成功但不重复产生待办。
                if version["status"] == v.VERSION_PENDING:
                    return version, f"v{version_no} 已在审批待办中，未重复生成待办"
                return None, f"v{version_no} 当前为{version['status']}，只有草稿能提交审批"
            if v.version_completeness(version) < len((*v.STRUCTURAL_FIELDS, "源表名称")):
                return None, f"v{version_no} 自然键列、代理键列、缓慢变化策略与源表名称未填全，不能提交审批"
            version["status"] = v.VERSION_PENDING
            version["history"].append({"action": "提交审批", "at": now, "by": "维度管理员", "note": "进入审批待办"})
            v.refresh_header(header)
            return version, f"v{version_no} 已提交审批"

        if action == "发布维度":
            if version["status"] != v.VERSION_PENDING:
                return None, (
                    f"不能跳级发布：v{version_no} 当前为{version['status']}，"
                    "版本必须先提交审批、审批通过后才能发布"
                )
            # 区间合法性与同链区间冲突。
            interval_error = v.validate_interval(version)
            if interval_error:
                return None, interval_error
            if version.get("backfill_of"):
                conflicts = v.backfill_conflicts(header, version)
            else:
                conflicts = v.interval_conflicts(header, version)
            if conflicts:
                labels = "、".join(
                    f"v{item['version_no']}（{item.get('生效日期') or '…'} ~ {item.get('失效日期') or '至今'}）"
                    for item in conflicts
                )
                return None, f"v{version_no} 生效区间与在档版本 {labels} 冲突，请先调整区间或改用回填"
            self._publish_version(header, version, now)
            v.refresh_header(header)
            return version, f"v{version_no} 已发布生效，历史版本按区间留档"

        if action == "废弃版本":
            if version["status"] in v.VERSION_CLOSED:
                return None, f"v{version_no} 已{version['status']}，无需重复废弃"
            version["status"] = v.VERSION_ARCHIVED
            version["history"].append({"action": "废弃版本", "at": now, "by": "维度管理员", "note": "版本归档停用并留档"})
            v.refresh_header(header)
            return version, f"v{version_no} 已废弃归档"

        return None, f"动作「{action}」不属于维度版本可执行范围"

    def _publish_version(self, header: dict[str, Any], version: dict[str, Any], now: str) -> None:
        version["status"] = v.VERSION_ACTIVE
        version["history"].append({"action": "发布维度", "at": now, "by": "审批人", "note": "审批通过并发布"})
        start = v.parse_day(version.get("生效日期"))

        if version.get("backfill_of"):
            # 回填发布：只关闭基准版本在回填区间内的效力，基准版本其余区间保留。
            base = v.find_version(header, int(version["backfill_of"]))
            if base is not None:
                base["history"].append({
                    "action": "回填生效", "at": now, "by": "系统",
                    "note": f"v{version['version_no']} 回填区间生效，本版本区间被收窄但记录保留",
                })
            return

        # 常规发布：旧在档版本按区间关闭，全部留档不删除。
        for older in v.effective_versions(header):
            if int(older["version_no"]) == int(version["version_no"]):
                continue
            if older["status"] != v.VERSION_ACTIVE:
                continue  # 已失效/归档的历史版本保持原留档结论
            older_end = v.parse_day(older.get("失效日期"))
            if older_end is None:
                # 开放区间的当前版本：收敛到新版本生效前一天。
                if start is not None:
                    older["失效日期"] = date.fromordinal(start.toordinal() - 1).isoformat()
            older["status"] = v.VERSION_EXPIRED
            older["history"].append({
                "action": "区间关闭", "at": now, "by": "系统",
                "note": f"v{version['version_no']} 生效，本版本按生效区间失效留档",
            })

    # ---------- 整维度废弃 ----------
    def discard_dimension(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        header = store.find(MODULE, entry_id)
        if header is None:
            return None, f"维度定义 {entry_id} 不存在或已归档"
        now = _now()
        for version in v.chain_versions(header):
            if version["status"] not in v.VERSION_CLOSED:
                version["status"] = v.VERSION_ARCHIVED
                version["history"].append({"action": "废弃维度", "at": now, "by": "维度管理员", "note": "维度整体废弃，版本归档留档"})
        header["status"] = "已废弃"
        header["pending"] = False
        header["abnormal"] = True
        return header, "维度已废弃，全部版本归档留档"

    # ---------- 兼容旧动作入口 ----------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        header = store.find(MODULE, entry_id)
        if header is None:
            return None, f"维度定义 {entry_id} 不存在或已归档"
        if action == "废弃维度":
            return self.discard_dimension(entry_id)
        tip = v.tip_version(header)
        if tip is None:
            return None, "维度下没有任何版本"
        return self.run_version_action(entry_id, int(tip["version_no"]), action)
