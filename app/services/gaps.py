from app.db import get_db, immediate_tx
from app.services.ready import POLICY, readiness_snapshot

# 对账口径（钉死，页面说明与此同源）：
#   expected  = 截至日应有盖印的批次（未作废）
#   stamped   = 真正有效盖印（已盖印，且其上游边全部有效）
#   review    = 候审只读（留存但不算有效盖印）
#   blocked   = 因源未齐备而无法盖印的在途批次
#   gap       = expected - stamped
# 空边印既不算 stamped，也不能被静默抹掉；它计入 review，缺口不虚低。


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM gap_reports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip() or "2026-11-16"
    with immediate_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_batches WHERE biz_date<=? AND status!='已作废' ORDER BY id",
            (day,),
        ).fetchall()

        expected = len(rows)
        stamped = 0
        review = 0
        blocked = 0
        empty_edge_codes = []
        for row in rows:
            snap = readiness_snapshot(conn, row)
            if row["status"] == "已盖印":
                if snap["ready"]:
                    stamped += 1
                else:
                    # 空边印：不应出现（有闸门兜底），出现即计候审并留证。
                    review += 1
                    empty_edge_codes.append(row["run_code"])
            elif row["status"] == "候审只读":
                review += 1
            else:
                blocked += 1

        gap = expected - stamped
        summary = (
            f"截至 {day}：应盖印 {expected}，有效盖印 {stamped}，缺口 {gap}"
            f"（候审 {review}，未齐备受阻 {blocked}）"
        )
        if empty_edge_codes:
            summary += "；空边印留证：" + "、".join(empty_edge_codes)

        cur = conn.execute(
            "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, "
            "gap_count, blocked_count, review_count, summary) VALUES (?,?,?,?,?,?,?)",
            (day, expected, stamped, gap, blocked, review, summary),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def reconcile(db_path: str | None = None, as_of_date: str = "2026-11-16") -> dict:
    """两轮复验用的对账明细：逐批给出为什么算/不算有效盖印。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_batches WHERE biz_date<=? AND status!='已作废' ORDER BY id",
            (as_of_date,),
        ).fetchall()
        items = []
        for row in rows:
            snap = readiness_snapshot(conn, row)
            if row["status"] == "已盖印" and snap["ready"]:
                bucket = "有效盖印"
            elif row["status"] in ("已盖印", "候审只读"):
                bucket = "候审只读"
            else:
                bucket = "未齐备受阻"
            items.append(
                {
                    "batch_id": row["id"],
                    "run_code": row["run_code"],
                    "table_name": row["table_name"],
                    "status": row["status"],
                    "watermark_value": row["watermark_value"],
                    "bucket": bucket,
                    "readiness": snap,
                }
            )
        return {"as_of_date": as_of_date, "policy": POLICY, "items": items}
