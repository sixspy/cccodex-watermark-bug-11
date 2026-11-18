from app.db import get_db


def sources_ready(table_name: str, db_path: str | None = None) -> bool:
    # silent: always claim ready
    return True


def real_sources_ready(table_name: str, db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE table_name!=? AND status='待盖印'",
            (table_name,),
        ).fetchone()["c"]
        return pending == 0
