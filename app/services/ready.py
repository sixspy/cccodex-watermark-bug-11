"""齐备判定：唯一真相源。

写印卡片、齐备席、靶表附页、血缘边目录全部读这里同一套裁定；
点写印时后端也调这里裁定，不对齐即失败。

钉死的工程命运（二选一已选定，勿再漂移）：
    源表未齐备却已有靶表写印 → **留存该写印、转入「候审只读」、拒绝继续写印**。
    不在写印时静默回滚；空边水印留在库中但被标记为候审，任何追索/缺口都不再
    把它当作有效盖印。
"""

from app.db import get_db

POLICY = {
    "code": "quarantine_retain",
    "label": "留存候审",
    "text": "源未齐备的空边写印留存入库并转候审只读，拒绝继续写印",
}

VALID_SOURCE_STATUS = "已盖印"
QUARANTINE_STATUS = "候审只读"
PENDING_STATUS = "待盖印"
VOID_STATUS = "已作废"


def _upstream_rows(conn, run_code: str):
    return conn.execute(
        """
        SELECT e.upstream_run_code AS run_code,
               b.id AS batch_id,
               b.table_name,
               b.status,
               b.watermark_value
          FROM lineage_edges e
          LEFT JOIN run_batches b ON b.run_code = e.upstream_run_code
         WHERE e.downstream_run_code = ?
         ORDER BY e.upstream_run_code
        """,
        (run_code,),
    ).fetchall()


def readiness_snapshot(conn, batch) -> dict:
    """给定连接与批次行，算齐备快照；不做任何写操作。"""
    upstream = []
    blockers = []
    for row in _upstream_rows(conn, batch["run_code"]):
        status = row["status"]
        if row["batch_id"] is None:
            reason = "SOURCE_MISSING"
            text = f"上游批次 {row['run_code']} 不存在"
        elif status == VALID_SOURCE_STATUS:
            reason = None
            text = "已盖印"
        elif status == QUARANTINE_STATUS:
            reason = "SOURCE_QUARANTINED"
            text = f"上游批次 {row['run_code']} 处于候审只读"
        elif status == VOID_STATUS:
            reason = "SOURCE_VOID"
            text = f"上游批次 {row['run_code']} 已作废"
        else:
            reason = "SOURCE_NOT_STAMPED"
            text = f"上游批次 {row['run_code']} 尚未盖印"

        item = {
            "run_code": row["run_code"],
            "batch_id": row["batch_id"],
            "table_name": row["table_name"],
            "status": status,
            "watermark_value": row["watermark_value"],
            "ready": reason is None,
            "reason_code": reason,
            "reason_text": text,
        }
        upstream.append(item)
        if reason is not None:
            blockers.append(
                {
                    "run_code": row["run_code"],
                    "table_name": row["table_name"],
                    "status": status,
                    "reason_code": reason,
                    "reason_text": text,
                }
            )

    return {
        "ready": len(blockers) == 0,
        "source_count": len(upstream),
        "ready_source_count": len(upstream) - len(blockers),
        "blockers": blockers,
        "upstream": upstream,
        "policy": POLICY,
    }


def _template_state(conn, table_name: str) -> dict:
    tpl = conn.execute(
        "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()
    return {
        "ok": tpl is not None,
        "version_no": tpl["version_no"] if tpl else None,
        "column_name": tpl["column_name"] if tpl else None,
        "reason_code": None if tpl else "NO_CURRENT_TEMPLATE",
    }


def fate_for(batch, snapshot: dict, template_ok: bool) -> dict:
    """命运标记：齐备席与写印卡片共用，文案与代码不落两处。"""
    status = batch["status"]
    if status == VALID_SOURCE_STATUS:
        if snapshot["ready"]:
            return {"code": "STAMPED_VALID", "label": "已盖印", "tone": "ok"}
        return {
            "code": "QUARANTINE_PENDING",
            "label": "待转候审",
            "tone": "warn",
        }
    if status == QUARANTINE_STATUS:
        return {
            "code": "QUARANTINED_READ_ONLY",
            "label": "候审只读",
            "tone": "review",
        }
    if status == VOID_STATUS:
        return {"code": "VOIDED", "label": "已作废", "tone": "muted"}
    if not snapshot["ready"]:
        return {"code": "HOLD_UNTIL_READY", "label": "等源齐备", "tone": "warn"}
    if not template_ok:
        return {"code": "NO_TEMPLATE", "label": "无现行模板", "tone": "bad"}
    return {"code": "WRITABLE", "label": "可写印", "tone": "ok"}


def assess_batch(conn, batch) -> dict:
    """一次裁定：齐备、模板、是否可写印、拒因。后端与页面共用。"""
    snapshot = readiness_snapshot(conn, batch)
    template = _template_state(conn, batch["table_name"])
    fate = fate_for(batch, snapshot, template["ok"])
    status = batch["status"]

    reason = None
    reason_text = None
    if status == QUARANTINE_STATUS:
        reason, reason_text = "BATCH_QUARANTINED", "批次处于候审只读，拒绝继续写印"
    elif status == VALID_SOURCE_STATUS and not snapshot["ready"]:
        # 遗留空边印：先说清真实原因（源未齐备），再说处置（转候审）。
        names = "、".join(b["reason_text"] for b in snapshot["blockers"])
        reason = "QUARANTINE_PENDING"
        reason_text = f"源表未齐备却已落印（{names}），应转候审只读，拒绝继续写印"
    elif status == VALID_SOURCE_STATUS:
        reason, reason_text = "ALREADY_STAMPED", "批次已盖印，不能重复写印"
    elif status == VOID_STATUS:
        reason, reason_text = "BATCH_VOID", "批次已作废，不能写印"
    elif not snapshot["ready"]:
        reason = "SOURCE_NOT_READY"
        names = "、".join(b["reason_text"] for b in snapshot["blockers"])
        reason_text = f"源表尚未齐备：{names}"
    elif not template["ok"]:
        reason = "NO_CURRENT_TEMPLATE"
        reason_text = "该表无现行水印模板，不能写印"

    return {
        "batch_id": batch["id"],
        "run_code": batch["run_code"],
        "table_name": batch["table_name"],
        "status": status,
        "readiness": snapshot,
        "template": template,
        "fate": fate,
        "writable": reason is None,
        "reason_code": reason,
        "reason_text": reason_text,
    }


def sweep_unready_stamps(conn, operator: str = "system") -> list[dict]:
    """把“源未齐备却已盖印”的遗留空边印立即收进候审只读并留痕。

    必须与写印裁定同源：用 readiness_snapshot 判定，命中即改状态。
    """
    moved = []
    rows = conn.execute(
        "SELECT * FROM run_batches WHERE status=? ORDER BY id", (VALID_SOURCE_STATUS,)
    ).fetchall()
    for row in rows:
        snap = readiness_snapshot(conn, row)
        if snap["ready"]:
            continue
        conn.execute(
            "UPDATE run_batches SET status=? WHERE id=? AND status=?",
            (QUARANTINE_STATUS, row["id"], VALID_SOURCE_STATUS),
        )
        conn.execute(
            """
            INSERT INTO stamp_adjudications
                (batch_id, table_name, run_code, watermark_value, verdict,
                 reason_code, reason_text, fate, readiness_snapshot, operator)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["id"],
                row["table_name"],
                row["run_code"],
                row["watermark_value"],
                "rejected",
                "QUARANTINED_UNREADY",
                "源表未齐备的空边写印，转候审只读并拒绝继续写印",
                QUARANTINE_STATUS,
                _dump_snapshot(snap),
                operator,
            ),
        )
        moved.append(
            {
                "batch_id": row["id"],
                "run_code": row["run_code"],
                "reason_code": "QUARANTINED_UNREADY",
                "fate": QUARANTINE_STATUS,
            }
        )
    return moved


def _dump_snapshot(snapshot: dict) -> str:
    import json

    return json.dumps(snapshot, ensure_ascii=False)


# ---- 不再保留的旧入口 -------------------------------------------------------
# 旧实现 sources_ready() 恒定返回 True，导致“未齐照写”。现一律走
# assess_batch / readiness_snapshot，避免两套判定。

def sources_ready(table_name: str, db_path: str | None = None) -> bool:
    """兼容旧调用：表级齐备 = 该表下所有待盖印批次都没有阻塞上游。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_batches WHERE table_name=? AND status=?",
            (table_name, PENDING_STATUS),
        ).fetchall()
        for row in rows:
            if not readiness_snapshot(conn, row)["ready"]:
                return False
        return True


def real_sources_ready(table_name: str, db_path: str | None = None) -> bool:
    return sources_ready(table_name, db_path)
