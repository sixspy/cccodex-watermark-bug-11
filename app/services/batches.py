import sqlite3

from app.db import get_db, immediate_tx
from app.services.errors import ServiceError
from app.services.ready import (
    POLICY,
    QUARANTINE_STATUS,
    assess_batch,
    readiness_snapshot,
    sweep_unready_stamps,
)

__all__ = [
    "list_batches",
    "get_batch",
    "get_batch_assessment",
    "stamp_batch",
    "list_adjudications",
]


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None


def get_batch_assessment(batch_id: int, db_path: str | None = None) -> dict | None:
    """写印卡片/靶表附页取数：同一套 assess_batch，与写印裁定同源。"""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not row:
            return None
        return assess_batch(conn, row)


def _insert_adjudication(
    conn,
    *,
    batch_id: int | None,
    table_name: str | None,
    run_code: str | None,
    watermark_value: str | None,
    verdict: str,
    reason_code: str,
    reason_text: str,
    fate: str | None,
    snapshot: dict | None,
    operator: str,
) -> None:
    import json

    conn.execute(
        """
        INSERT INTO stamp_adjudications
            (batch_id, table_name, run_code, watermark_value, verdict,
             reason_code, reason_text, fate, readiness_snapshot, operator)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            table_name,
            run_code,
            watermark_value,
            verdict,
            reason_code,
            reason_text,
            fate,
            json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
            operator,
        ),
    )


def _log_rejection(
    db_path: str | None,
    *,
    batch_id: int | None,
    table_name: str | None,
    run_code: str | None,
    watermark_value: str | None,
    reason_code: str,
    reason_text: str,
    fate: str | None,
    snapshot: dict | None,
    operator: str,
) -> None:
    try:
        with get_db(db_path) as conn:
            _insert_adjudication(
                conn,
                batch_id=batch_id,
                table_name=table_name,
                run_code=run_code,
                watermark_value=watermark_value,
                verdict="rejected",
                reason_code=reason_code,
                reason_text=reason_text,
                fate=fate,
                snapshot=snapshot,
                operator=operator,
            )
    except sqlite3.Error:
        # 拒因留痕失败不能掩盖真正的业务拒绝。
        pass


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
    operator: str = "admin",
) -> dict:
    """点写印：一次裁定 + 显式写锁，齐备与落印同事务。

    - 未齐备：拒绝写印并留拒因，不落库任何印。
    - 已盖印/候审只读/已作废：拒绝并留拒因。
    - 并发：BEGIN IMMEDIATE 先锁后判，后到者看到已盖印即拒，不会双写。
    """
    value = (watermark_value or "").strip()
    batch = None
    decision = None
    try:
        with immediate_tx(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM run_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if not row:
                raise ServiceError("批次不存在", 404, "BATCH_NOT_FOUND")
            batch = dict(row)
            if not value:
                raise ServiceError("水印值不能为空", 400, "EMPTY_WATERMARK")

            decision = assess_batch(conn, batch)
            if not decision["writable"]:
                raise ServiceError(
                    decision["reason_text"] or "不可写印",
                    409,
                    decision["reason_code"] or "NOT_WRITABLE",
                    {
                        "fate": decision["fate"],
                        "readiness": decision["readiness"],
                    },
                )

            try:
                conn.execute(
                    "UPDATE run_batches SET status='已盖印', watermark_value=?, "
                    "stamped_at=datetime('now') WHERE id=? AND status='待盖印'",
                    (value, batch_id),
                )
            except sqlite3.IntegrityError as exc:
                code = _integrity_code(exc)
                raise ServiceError(
                    "库级闸门拒绝写印：" + code, 409, code, {"fate": decision["fate"]}
                ) from exc

            updated = conn.execute(
                "SELECT * FROM run_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if updated["status"] != "已盖印":
                raise ServiceError(
                    "批次状态已被他人改写，写印未生效", 409, "STAMP_LOST_RACE"
                )

            snapshot = readiness_snapshot(conn, updated)
            _insert_adjudication(
                conn,
                batch_id=updated["id"],
                table_name=updated["table_name"],
                run_code=updated["run_code"],
                watermark_value=value,
                verdict="allowed",
                reason_code="STAMP_ALLOWED",
                reason_text="源表齐备，写印成功",
                fate="STAMPED_VALID",
                snapshot=snapshot,
                operator=operator,
            )
            return dict(updated)
    except ServiceError as exc:
        _log_rejection(
            db_path,
            batch_id=(batch or {}).get("id"),
            table_name=(batch or {}).get("table_name"),
            run_code=(batch or {}).get("run_code"),
            watermark_value=value,
            reason_code=exc.reason_code,
            reason_text=exc.message,
            fate=(decision or {}).get("fate", {}).get("code"),
            snapshot=(decision or {}).get("readiness"),
            operator=operator,
        )
        raise


def _integrity_code(exc: sqlite3.IntegrityError) -> str:
    text = str(exc)
    if "ERR_DOUBLE_STAMP" in text:
        return "ALREADY_STAMPED"
    if "ERR_SOURCE_NOT_READY" in text:
        return "SOURCE_NOT_READY"
    return "INTEGRITY_ERROR"


def list_adjudications(
    db_path: str | None = None, limit: int = 200
) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM stamp_adjudications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def readiness_board(db_path: str | None = None) -> dict:
    """齐备席 + 靶表附页 + 血缘边目录共用的裁定视图。"""
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        assessments = [assess_batch(conn, r) for r in rows]

        edges = []
        for e in conn.execute(
            """
            SELECT e.* FROM lineage_edges e
             ORDER BY e.downstream_run_code, e.upstream_run_code
            """
        ).fetchall():
            up = conn.execute(
                "SELECT * FROM run_batches WHERE run_code=?",
                (e["upstream_run_code"],),
            ).fetchone()
            down = conn.execute(
                "SELECT * FROM run_batches WHERE run_code=?",
                (e["downstream_run_code"],),
            ).fetchone()
            up_status = up["status"] if up else "缺失"
            down_status = down["status"] if down else "缺失"
            edges.append(
                {
                    "id": e["id"],
                    "upstream_run_code": e["upstream_run_code"],
                    "downstream_run_code": e["downstream_run_code"],
                    "upstream_status": up_status,
                    "downstream_status": down_status,
                    "upstream_valid": up_status == "已盖印",
                    "empty_edge": up_status != "已盖印",
                    "upstream_watermark": up["watermark_value"] if up else None,
                }
            )

    by_table: dict[str, list[dict]] = {}
    for a in assessments:
        by_table.setdefault(a["table_name"], []).append(a)

    target_tables = []
    for table_name, items in by_table.items():
        target_tables.append(
            {
                "table_name": table_name,
                "batch_count": len(items),
                "ready_count": sum(1 for i in items if i["readiness"]["ready"]),
                "blocked_count": sum(1 for i in items if not i["readiness"]["ready"]),
                "writable_count": sum(1 for i in items if i["writable"]),
                "quarantined_count": sum(
                    1 for i in items if i["status"] == QUARANTINE_STATUS
                ),
                "batches": items,
            }
        )

    return {
        "policy": POLICY,
        "assessments": assessments,
        "target_tables": target_tables,
        "edges": edges,
        "counts": {
            "batches": len(assessments),
            "writable": sum(1 for a in assessments if a["writable"]),
            "blocked": sum(1 for a in assessments if not a["readiness"]["ready"]),
            "quarantined": sum(
                1 for a in assessments if a["status"] == QUARANTINE_STATUS
            ),
            "empty_edges": sum(1 for e in edges if e["empty_edge"]),
        },
    }


def sweep_quarantine(db_path: str | None = None, operator: str = "system") -> list[dict]:
    with get_db(db_path) as conn:
        return sweep_unready_stamps(conn, operator=operator)
