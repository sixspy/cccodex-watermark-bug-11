from app.db import get_db
from app.services.errors import ServiceError
from app.services.ready import readiness_snapshot


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    """按水印值追索下游。

    空边印（源未齐备却已盖印 / 候审只读）不再被当成有效上游：
    它们仍出现在结果里供人工核查，但 `valid=True` 才会把下游算作可达。
    """
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空", 400, "EMPTY_WATERMARK")
    with get_db(db_path) as conn:
        sources = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources:
            raise ServiceError(
                "未找到该水印值的盖印批次", 404, "WATERMARK_NOT_FOUND"
            )

        source_items = []
        valid_codes = []
        empty_codes = []
        for s in sources:
            snap = readiness_snapshot(conn, s)
            valid = s["status"] == "已盖印" and snap["ready"]
            source_items.append(
                {
                    **dict(s),
                    "valid": valid,
                    "empty_edge": not valid,
                    "readiness": snap,
                }
            )
            if valid:
                valid_codes.append(s["run_code"])
            else:
                empty_codes.append(s["run_code"])

        edges = []
        downstream = []
        if valid_codes:
            placeholders = ",".join("?" for _ in valid_codes)
            edges = [
                dict(e)
                for e in conn.execute(
                    f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders}) "
                    "ORDER BY downstream_run_code",
                    valid_codes,
                ).fetchall()
            ]
            down_codes = sorted({e["downstream_run_code"] for e in edges})
            if down_codes:
                ph2 = ",".join("?" for _ in down_codes)
                downstream = [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                        down_codes,
                    ).fetchall()
                ]

        return {
            "watermark_value": value,
            "sources": source_items,
            "edges": edges,
            "downstream": downstream,
            "empty_edge_sources": empty_codes,
            "valid_source_count": len(valid_codes),
            "empty_source_count": len(empty_codes),
            # 明确告诉前端：这次追索有没有吃到空边印。
            "used_empty_edges": len(valid_codes) == 0 and len(empty_codes) > 0,
        }
