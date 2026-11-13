"""维度版本链与事实引用的共享领域规则。

维度定义页和事实表页必须基于同一套口径判断"某个维度版本当前是什么状态、事实在
目标日期实际命中哪个版本"，因此把这些规则收在本模块，两个 service 共同调用，避免
各算各的。

核心概念
--------
- 维度头（header）：一个「维度编号」对应一条头记录，挂着一条按 version_no 排列的版本链。
- 版本（version）：草稿 -> 待审批 -> 已生效 依次推进，已生效版本按各自生效区间留档，
  不能被覆盖，也不能跳级发布。
- 回填（backfill）：晚到的修正进入历史区间，生成独立的回填版本，不改写当前版本。
- 事实引用（ref）：事实通过维度编号 + 版本号锁定当时使用的维度版本，保证可追溯。
"""
from __future__ import annotations

from datetime import date
from typing import Any

# 影响维度"身份/口径"的字段：这些字段发生变化必须生成新版本，而不是覆盖旧定义。
STRUCTURAL_FIELDS = ("自然键列", "代理键列", "缓慢变化策略")
# 只调整区间不改变口径的字段，允许在已生效历史区间上做回填修正。
INTERVAL_FIELDS = ("生效日期", "失效日期")

VERSION_DRAFT = "草稿"
VERSION_PENDING = "待审批"
VERSION_ACTIVE = "已生效"
VERSION_EXPIRED = "已失效"
VERSION_ARCHIVED = "已归档"

# 版本只能沿这条链顺序推进，不允许跳级。
VERSION_FLOW = [VERSION_DRAFT, VERSION_PENDING, VERSION_ACTIVE]
# 终态：留档版本，只读。
VERSION_CLOSED = (VERSION_EXPIRED, VERSION_ARCHIVED)


def parse_day(value: Any) -> date | None:
    """把 'YYYY-MM-DD' 解析成 date；空值/非法值返回 None（开放区间端点）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def interval_covers(start: Any, end: Any, day: date) -> bool:
    """闭区间 [start, end] 是否覆盖 day；端点为空表示向过去/未来无限开放。"""
    left = parse_day(start)
    right = parse_day(end)
    if left is not None and day < left:
        return False
    if right is not None and day > right:
        return False
    return True


def intervals_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两个版本的生效区间是否重叠；空端点按开放处理。"""
    a_start, a_end = parse_day(a.get("生效日期")), parse_day(a.get("失效日期"))
    b_start, b_end = parse_day(b.get("生效日期")), parse_day(b.get("失效日期"))
    # a 完全在 b 左侧，或 b 完全在 a 左侧，则不重叠。
    if a_end is not None and b_start is not None and a_end < b_start:
        return False
    if b_end is not None and a_start is not None and b_end < a_start:
        return False
    return True


def version_completeness(version: dict[str, Any]) -> int:
    """版本完整度：结构字段与区间字段填得越全，完整度越高（用于冲突裁决）。"""
    fields = (*STRUCTURAL_FIELDS, *INTERVAL_FIELDS, "源表名称")
    return sum(1 for field in fields if str(version.get(field) or "").strip())


def version_identity(version: dict[str, Any]) -> tuple[str, str, str]:
    """结构口径指纹：自然键/代理键/缓慢变化策略。"""
    return tuple(str(version.get(field) or "").strip() for field in STRUCTURAL_FIELDS)  # type: ignore[return-value]


def find_header(rows: list[dict[str, Any]], dim_code: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("维度编号") == dim_code:
            return row
    return None


def find_version(header: dict[str, Any], version_no: int) -> dict[str, Any] | None:
    for version in header.get("versions", []):
        if int(version.get("version_no", 0)) == int(version_no):
            return version
    return None


def chain_versions(header: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(header.get("versions", []), key=lambda item: int(item.get("version_no", 0)))


def tip_version(header: dict[str, Any]) -> dict[str, Any] | None:
    versions = chain_versions(header)
    return versions[-1] if versions else None


def refresh_header(header: dict[str, Any]) -> dict[str, Any]:
    """按版本链回算维度头的汇总状态，保证列表与版本链是同一份结论。"""
    versions = chain_versions(header)
    if not versions:
        header["status"] = VERSION_DRAFT
        header["pending"] = True
        return header
    tip = versions[-1]
    # 头状态优先反映链尾正在推进的版本；链尾已落定则看是否仍有在档生效版本。
    if tip["status"] in (VERSION_DRAFT, VERSION_PENDING):
        header["status"] = tip["status"]
    elif any(version["status"] == VERSION_ACTIVE for version in versions):
        header["status"] = VERSION_ACTIVE
    else:
        header["status"] = tip["status"]
    header["pending"] = any(version["status"] == VERSION_PENDING for version in versions)
    header["abnormal"] = bool(header.get("abnormal"))
    header["current_version_no"] = tip["version_no"]
    # 头表上的字段始终镜像链尾版本，老页面字段不致取空。
    for field in ("源表名称", *STRUCTURAL_FIELDS, "生效日期", "失效日期"):
        header[field] = tip.get(field)
    return header


def effective_versions(header: dict[str, Any]) -> list[dict[str, Any]]:
    return [v for v in chain_versions(header) if v.get("status") in (VERSION_ACTIVE, VERSION_EXPIRED)]


def version_at(header: dict[str, Any], day: date) -> dict[str, Any] | None:
    """维度在指定日期实际生效的版本（含已失效但当时在档的版本）。

    同一区间若存在回填版本，回填版本（backfill_of 非空）优先于被回填的原始版本。
    """
    candidates = [
        version
        for version in effective_versions(header)
        if interval_covers(version.get("生效日期"), version.get("失效日期"), day)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            1 if item.get("backfill_of") else 0,  # 回填修正优先
            parse_day(item.get("生效日期")) or date.min,
            int(item.get("version_no", 0)),
        ),
        reverse=True,
    )
    return candidates[0]


def structural_changed(base: dict[str, Any], values: dict[str, Any]) -> bool:
    return any(
        str(values.get(field) if values.get(field) is not None else "").strip()
        != str(base.get(field) or "").strip()
        for field in STRUCTURAL_FIELDS
        if str(values.get(field) or "").strip()
    )


def next_version_no(header: dict[str, Any]) -> int:
    return max((int(v.get("version_no", 0)) for v in header.get("versions", [])), default=0) + 1


def validate_interval(values: dict[str, Any]) -> str | None:
    """校验生效/失效日期的格式与先后顺序。"""
    start = parse_day(values.get("生效日期"))
    end = parse_day(values.get("失效日期"))
    if values.get("生效日期") and start is None:
        return "生效日期格式应为 YYYY-MM-DD"
    if values.get("失效日期") and end is None:
        return "失效日期格式应为 YYYY-MM-DD"
    if start is not None and end is not None and end < start:
        return "失效日期不能早于生效日期"
    return None


def _archived_bounded_versions(header: dict[str, Any]) -> list[dict[str, Any]]:
    """链上所有带闭合失效日期的留档版本（含已失效/已归档），用于区间占位判断。"""
    return [
        version
        for version in chain_versions(header)
        if parse_day(version.get("失效日期")) is not None
        and version.get("status") in (VERSION_ACTIVE, VERSION_EXPIRED, VERSION_ARCHIVED)
    ]


def interval_conflicts(header: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """常规版本发布时的区间冲突：拦截与「有界留档版本」的重叠。

    开放区间（失效日期为空）的当前生效版本与新版本重叠属于正常接班，发布时会自动
    把旧版本收敛到新版本生效前一天，不算冲突；真正的冲突是新版本落进了某个已经
    闭合的历史区间（含已失效/已归档版本的留档区间）。
    """
    conflicts = []
    for version in _archived_bounded_versions(header):
        if int(version.get("version_no", 0)) == int(candidate.get("version_no", -1)):
            continue
        if intervals_overlap(version, candidate):
            conflicts.append(version)
    return conflicts


def backfill_conflicts(header: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """回填版本发布冲突：同一基准版本上已生效的其他回填区间不得重叠。"""
    base_no = candidate.get("backfill_of")
    conflicts = []
    for version in chain_versions(header):
        if int(version.get("version_no", 0)) == int(candidate.get("version_no", -1)):
            continue
        if version.get("backfill_of") == base_no and version["status"] == VERSION_ACTIVE \
                and intervals_overlap(version, candidate):
            conflicts.append(version)
    return conflicts


def resolve_ref(
    dim_rows: list[dict[str, Any]],
    dim_code: str,
    version_no: int | None,
    target_day: date,
) -> dict[str, Any]:
    """事实表页与维度列表共用的引用解析口径。

    返回事实实际选中版本的状态、是否覆盖目标日期、失效结论等，事实页直接渲染这份
    结论，不再自行判断，保证"同一版本、同一失效结论"。
    """
    header = find_header(dim_rows, dim_code)
    if header is None:
        return {
            "维度编号": dim_code,
            "version_no": version_no,
            "ok": False,
            "reason": "维度不存在或已删除",
            "conclusion": "引用缺失",
        }
    selected = find_version(header, version_no) if version_no is not None else None
    if selected is None:
        return {
            "维度编号": dim_code,
            "version_no": version_no,
            "ok": False,
            "reason": "未选中具体维度版本",
            "conclusion": "引用缺失",
        }
    status = selected["status"]
    covers = interval_covers(selected.get("生效日期"), selected.get("失效日期"), target_day)
    effective_now = version_at(header, target_day)
    reasons = []
    ok = True
    if status == VERSION_DRAFT:
        ok = False
        reasons.append("选中版本仍是草稿，尚未进入审批")
    elif status == VERSION_PENDING:
        ok = False
        reasons.append("选中版本待审批，尚未生效")
    elif status == VERSION_ARCHIVED:
        ok = False
        reasons.append("选中版本已归档停用")
    if ok and not covers:
        ok = False
        reasons.append(f"选中版本生效区间不覆盖目标日期 {target_day.isoformat()}")
    # 区间冲突：同一自然键在目标日期还能命中另一条在档版本。
    if ok and effective_now is not None and int(effective_now["version_no"]) != int(selected["version_no"]):
        ok = False
        reasons.append(
            f"目标日期实际命中 v{effective_now['version_no']}，与选中 v{selected['version_no']} 区间冲突"
        )
    conclusion = "引用有效" if ok else "不可发布"
    return {
        "维度编号": dim_code,
        "维度名称": header.get("维度名称"),
        "version_no": selected["version_no"],
        "version_status": status,
        "生效日期": selected.get("生效日期"),
        "失效日期": selected.get("失效日期"),
        "自然键列": selected.get("自然键列"),
        "缓慢变化策略": selected.get("缓慢变化策略"),
        "covers_target": covers,
        "effective_version_no": effective_now["version_no"] if effective_now else None,
        "ok": ok,
        "reason": "；".join(reasons) if reasons else "版本在档且区间覆盖目标日期",
        "conclusion": conclusion,
    }


def arbitrate(
    dim_rows: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    target_day: date,
) -> list[dict[str, Any]]:
    """对引用相同自然键不同版本的事实做优先级裁决。

    裁决顺序（逐级比较，可追溯）：
    1. 生效区间覆盖目标日期者优先（冲突时以可追溯且区间覆盖目标日期的一侧为准）；
    2. 生效时间更早、留档可追溯时间更长者优先；
    3. 版本完整度更高者优先；
    4. 事实批次更早者优先。
    """
    # 收集 (事实, 引用解析结果, 自然键) 三元组。
    indexed: list[dict[str, Any]] = []
    for fact in facts:
        for ref in fact.get("维度引用", []):
            resolved = resolve_ref(dim_rows, ref.get("维度编号"), ref.get("version_no"), target_day)
            header = find_header(dim_rows, ref.get("维度编号"))
            version = find_version(header, ref["version_no"]) if header else None
            indexed.append({
                "事实编号": fact.get("事实编号"),
                "事实批次": fact.get("事实批次") or "",
                "ref": ref,
                "resolved": resolved,
                "natural_key": (version or {}).get("自然键列") or ref.get("自然键列") or "",
                "version": version,
            })
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in indexed:
        if item["natural_key"]:
            groups.setdefault(item["natural_key"], []).append(item)

    reports = []
    for natural_key, items in groups.items():
        distinct = {
            (entry["ref"].get("维度编号"), entry["ref"].get("version_no"))
            for entry in items
        }
        if len(distinct) <= 1:
            continue  # 同一自然键只引用了同一个版本，不存在冲突。

        def sort_key(entry: dict[str, Any]) -> tuple:
            version = entry["version"] or {}
            start = parse_day(version.get("生效日期")) or date.max
            # 升序排序键：覆盖目标日期者在前、生效更早在前、完整度更高在前、批次更早在前。
            return (
                0 if entry["resolved"].get("covers_target") else 1,
                start,
                -version_completeness(version),
                str(entry["事实批次"]),
            )

        ranked = sorted(items, key=sort_key)
        winner = ranked[0]
        reports.append({
            "自然键列": natural_key,
            "目标日期": target_day.isoformat(),
            "winner": {
                "事实编号": winner["事实编号"],
                "维度编号": winner["ref"].get("维度编号"),
                "version_no": winner["ref"].get("version_no"),
                "事实批次": winner["事实批次"],
            },
            "candidates": [
                {
                    "事实编号": entry["事实编号"],
                    "事实批次": entry["事实批次"],
                    "维度编号": entry["ref"].get("维度编号"),
                    "version_no": entry["ref"].get("version_no"),
                    "version_status": entry["resolved"].get("version_status"),
                    "covers_target": entry["resolved"].get("covers_target"),
                    "completeness": version_completeness(entry["version"] or {}),
                    "生效日期": (entry["version"] or {}).get("生效日期"),
                }
                for entry in ranked
            ],
            "rule": "依次按 生效区间覆盖目标日期 > 生效时间更早 > 版本完整度更高 > 事实批次更早 裁决",
            "reason": (
                f"获胜方 {winner['事实编号']} 引用 "
                f"{winner['ref'].get('维度编号')} v{winner['ref'].get('version_no')}："
                f"{'区间覆盖目标日期且可追溯' if winner['resolved'].get('covers_target') else '同条件下按生效时间/完整度/批次领先'}"
            ),
        })
    return reports
