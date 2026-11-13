"""后端规则自检：版本链、回填、发布阻断、待办去重与冲突裁决。

运行：cd backend && .venv/bin/python -m app.verify_rules
"""
from __future__ import annotations

import copy

from app.services.dimdef import DimdefService
from app.services.factdef import FactdefService
from app.store import store
from app import versioning as v

PASS = "✅"
FAIL = "❌"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL} {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def clone_dimension(service: DimdefService, source_code: str, new_code: str) -> dict:
    """深拷贝一条种子维度做破坏性试验，避免污染事实引用与裁决用的种子数据。"""
    rows = store.rows("dimdef")
    source = v.find_header(rows, source_code)
    clone = copy.deepcopy(source)
    clone["id"] = max(int(row["id"]) for row in rows) + 1
    clone["维度编号"] = new_code
    clone["维度名称"] = f"{source['维度名称']}（试验副本）"
    rows.append(clone)
    return v.refresh_header(clone)


def main() -> None:
    dim = DimdefService()
    fact = FactdefService()
    dim_rows = store.rows("dimdef")
    fact_rows = store.rows("factdef")

    print("== 1. 种子：DIMD-0002 待审批 / DIMD-0003 已生效版本链 ==")
    d2 = dim.get_by_code("DIMD-0002")
    d3 = dim.get_by_code("DIMD-0003")
    check("DIMD-0002 头状态为待审批", d2["status"] == "待审批")
    check("DIMD-0002 链尾 v2 为待审批，v1 仍已生效留档",
          v.find_version(d2, 2)["status"] == "待审批" and v.find_version(d2, 1)["status"] == "已生效")
    check("DIMD-0003 头状态为已生效", d3["status"] == "已生效")
    statuses = [item["status"] for item in v.chain_versions(d3)]
    check("DIMD-0003 历史版本按区间留档（已归档/已失效/已生效）",
          statuses == ["已归档", "已失效", "已生效"], str(statuses))

    # 破坏性试验全部在副本上进行，种子维度保持不动。
    lab = clone_dimension(dim, "DIMD-0003", "DIMD-0099")
    lab_id = lab["id"]

    print("== 2. 结构变更生成新版本，不覆盖已生效版本 ==")
    before = v.find_version(lab, 3)
    before_nk = before["自然键列"]
    new_ver, msg, kind = dim.edit_version(lab_id, 3, {
        "自然键列": "customer_global_id", "change_note": "自然键口径升级",
    })
    check("在已生效 v3 上改自然键生成新草稿", kind == "spawned" and new_ver["version_no"] == 4)
    check("原 v3 自然键未被覆盖", v.find_version(lab, 3)["自然键列"] == before_nk)
    check("v4 为草稿且自然键已更新", new_ver["status"] == "草稿" and new_ver["自然键列"] == "customer_global_id")

    print("== 3. 不能覆盖已生效版本：纯日期改在档版本被拒，须走回填 ==")
    _, msg2, kind2 = dim.edit_version(lab_id, 2, {"失效日期": "2026-09-15"})
    check("在档版本就地改日期被拒绝", kind2 == "rejected" and "回填" in msg2, msg2)

    print("== 4. 草稿可就地编辑；再次结构变更并入同一草稿 ==")
    updated, _, kind3 = dim.edit_version(lab_id, 4, {"代理键列": "gk_v2"})
    check("草稿 v4 就地更新", kind3 == "updated" and updated["代理键列"] == "gk_v2")
    merged, _, kind4 = dim.edit_version(lab_id, 3, {"缓慢变化策略": "SCD3"})
    check("链尾已有草稿时结构变更并入 v4，不新增草稿", kind4 == "spawned" and merged["version_no"] == 4)
    check("链上仍是 4 个版本", len(v.chain_versions(lab)) == 4)

    print("== 5. 状态只能顺序推进，不能跳级发布；待办去重 ==")
    _, msg3 = dim.run_version_action(lab_id, 4, "发布维度")
    check("草稿直接发布被阻断（跳级）", "跳级" in msg3, msg3)
    _, msg4 = dim.run_version_action(lab_id, 4, "提交审批")
    check("v4 提交审批成功", "已提交审批" in msg4)
    _, msg5 = dim.run_version_action(lab_id, 4, "提交审批")
    check("重复提交审批幂等，不重复产生待办", "未重复生成待办" in msg5, msg5)
    todos = dim.list_todos()
    lab_todos = [t for t in todos if t["维度编号"] == "DIMD-0099" and t["version_no"] == 4]
    check("待办列表中试验 v4 只有一份", len(lab_todos) == 1)
    check("待办包含 DIMD-0002 v2", any(t["维度编号"] == "DIMD-0002" and t["version_no"] == 2 for t in todos))

    print("== 6. 区间冲突阻止发布；错开后按接班收敛旧版本 ==")
    # v4 落在有界历史版本 v2 的区间内 → 真冲突，必须阻断。
    dim.edit_version(lab_id, 4, {"生效日期": "2026-05-01", "失效日期": "2026-06-30"})
    _, msg6 = dim.run_version_action(lab_id, 4, "发布维度")
    check("落入有界历史区间的 v4 发布被阻断", "冲突" in msg6, msg6)
    # 调整为开放接班区间后允许发布，v3 自动收敛失效日期。
    dim.edit_version(lab_id, 4, {"生效日期": "2027-01-01", "失效日期": ""})
    published, msg7 = dim.run_version_action(lab_id, 4, "发布维度")
    check("区间错开后 v4 发布成功", published is not None and published["status"] == "已生效", msg7)
    check("v3 按区间失效留档", v.find_version(lab, 3)["status"] == "已失效")
    check("v3 失效日期自动收敛到 v4 生效前一天", v.find_version(lab, 3)["失效日期"] == "2026-12-31")

    print("== 7. 晚到修正回填到所属历史区间，不改写当前版本 ==")
    current_nk = v.find_version(lab, 4)["自然键列"]
    bf, msg8 = dim.backfill_version(lab_id, 2, {
        "生效日期": "2026-03-01", "失效日期": "2026-05-31",
        "代理键列": "dim_customer_key_bf", "change_note": "晚到的代理键修正",
    })
    check("回填草稿生成并挂在 v2 下", bf is not None and bf["backfill_of"] == 2, msg8)
    check("回填区间越界被拒", dim.backfill_version(lab_id, 2, {
        "生效日期": "2024-01-01", "失效日期": "2024-02-01"})[0] is None)
    dim.run_version_action(lab_id, bf["version_no"], "提交审批")
    bf_pub, msg9 = dim.run_version_action(lab_id, bf["version_no"], "发布维度")
    check("回填版本审批发布成功", bf_pub is not None and bf_pub["status"] == "已生效", msg9)
    # 同一基准版本上重叠的第二个回填必须被拦。
    bf2, _ = dim.backfill_version(lab_id, 2, {"生效日期": "2026-04-01", "失效日期": "2026-04-30"})
    dim.run_version_action(lab_id, bf2["version_no"], "提交审批")
    _, msg_bf = dim.run_version_action(lab_id, bf2["version_no"], "发布维度")
    check("同基准版本重叠回填发布被阻断", "冲突" in msg_bf, msg_bf)
    check("当前版本 v4 未被回填改写", v.find_version(lab, 4)["自然键列"] == current_nk)
    check("v2 记录仍保留（未被删除/覆盖）", v.find_version(lab, 2) is not None)
    hit = v.version_at(lab, v.parse_day("2026-04-15"))
    check("回填区间内日期命中回填版本", hit is not None and hit["version_no"] == bf["version_no"])
    hit_outside = v.version_at(lab, v.parse_day("2026-07-01"))
    check("回填区间外仍命中 v2", hit_outside is not None and hit_outside["version_no"] == 2)

    print("== 8. 事实页显示实际选中版本，结论与维度页同源 ==")
    f2 = fact.get_entry(next(r["id"] for r in fact_rows if r["事实编号"] == "FACT-0002"))
    ref2 = f2["维度版本状态"][0]
    check("FACT-0002 显示选中 DIMD-0002 v2（待审批）",
          ref2["维度编号"] == "DIMD-0002" and ref2["version_no"] == 2 and ref2["version_status"] == "待审批")
    check("FACT-0002 引用结论为不可发布", ref2["ok"] is False and "待审批" in ref2["reason"])

    print("== 9. 待发布事实引用未生效维度版本时阻止发布，生效后放行 ==")
    f2_id = f2["id"]
    _, msg10 = fact.run_action(f2_id, "发布事实")
    check("FACT-0002 发布被阻断并给出原因", msg10.startswith("发布被阻断"), msg10)
    check("publish-check 显式 blocked", fact.publish_check(f2_id)["blocked"] is True)
    dim.run_version_action(d2["id"], 2, "发布维度")
    check("DIMD-0002 v2 发布后 v1 按区间失效", v.find_version(d2, 1)["status"] == "已失效")
    _, msg11 = fact.run_action(f2_id, "发布事实")
    check("维度生效且区间覆盖今天后 FACT-0002 发布成功",
          msg11 == "事实定义已发布，引用的维度版本已随事实留档", msg11)

    print("== 10. 不能跳级发布事实 ==")
    f1 = next(r for r in fact_rows if r["事实编号"] == "FACT-0001")
    _, msg12 = fact.run_action(f1["id"], "发布事实")
    check("草稿事实直接发布被阻断", "跳级" in msg12, msg12)

    print("== 11. 维度状态同步到事实清单（同一版本/同一失效结论） ==")
    listed, _ = fact.list_entries(page=1, size=100)
    f3_row = next(row for row in listed if row["事实编号"] == "FACT-0003")
    check("清单中 FACT-0003 带维度版本结论", len(f3_row["维度版本状态"]) == 1 and f3_row["引用可发布"] is True)
    check("清单与明细使用同一解析口径", f3_row["维度版本状态"][0]["conclusion"] == "引用有效")
    f3_detail = fact.get_entry(f3_row["id"])
    check("明细与清单的失效结论完全一致",
          f3_detail["维度版本状态"][0]["reason"] == f3_row["维度版本状态"][0]["reason"])

    print("== 12. 同自然键不同版本引用的冲突裁决 ==")
    report = fact.arbitration_report(target="2026-11-13")
    check("裁决报告识别出 customer_id 冲突", report["total"] >= 1, str(report))
    conflict = next((c for c in report["conflicts"] if c["自然键列"] == "customer_id"), None)
    check("裁决包含 FACT-0003 与 FACT-0004 两方", conflict is not None and
          {c["事实编号"] for c in conflict["candidates"]} == {"FACT-0003", "FACT-0004"})
    check("两侧均覆盖目标日期时，生效更早的 FACT-0003 获胜",
          conflict is not None and conflict["winner"]["事实编号"] == "FACT-0003",
          conflict["reason"] if conflict else "")
    check("裁决给出可追溯规则说明", conflict is not None and "生效区间覆盖" in conflict["rule"])

    print("== 13. 引用锁定与区间冲突解析 ==")
    # FACT-0001 引用 DIMD-0001 v1 草稿：切到一个已失效的历史版本应提示区间不覆盖。
    fact.set_reference(f1["id"], "DIMD-0003", 2)  # v2 区间 2026-01-01~2026-08-31
    check_resp = fact.publish_check(f1["id"], target="2026-11-13")
    check("引用已失效历史版本时给出区间不覆盖结论",
          any("不覆盖目标日期" in reason for reason in check_resp["reasons"]), str(check_resp["reasons"]))
    hit_today = v.resolve_ref(dim_rows, "DIMD-0003", 3, v.parse_day("2026-11-13"))
    check("DIMD-0003 v3 在今天仍判定为引用有效", hit_today["ok"] is True, hit_today["reason"])

    print()
    if failures:
        print(f"{FAIL} 失败 {len(failures)} 项：{failures}")
        raise SystemExit(1)
    print(f"{PASS} 全部规则自检通过")


if __name__ == "__main__":
    main()
