"""SQLite 저장소. 분석 이력은 추가만 하며 수정·삭제 함수는 두지 않는다."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def _json(obj) -> str:
    # sort_keys: 같은 내용이면 같은 문자열 → 재현성 비교 가능
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def log(conn, action: str, entity: str, entity_id: int | None = None, detail: dict | None = None):
    conn.execute(
        "INSERT INTO audit_logs(action, entity, entity_id, detail_json) VALUES (?,?,?,?)",
        (action, entity, entity_id, _json(detail or {})),
    )


def create_company(conn, company_code: str, name: str, industry: str | None = None) -> int:
    with conn:
        cid = conn.execute(
            "INSERT INTO companies(company_code, name, industry) VALUES (?,?,?)",
            (company_code, name, industry),
        ).lastrowid
        log(conn, "create", "companies", cid)
    return cid


def create_case(conn, company_id: int, case_no: str, purpose: str | None = None) -> int:
    with conn:
        case_id = conn.execute(
            "INSERT INTO credit_cases(company_id, case_no, purpose) VALUES (?,?,?)",
            (company_id, case_no, purpose),
        ).lastrowid
        log(conn, "create", "credit_cases", case_id)
    return case_id


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def store_document(conn, company_id: int, src: Path | str, raw_dir: Path | str) -> int:
    """원본 파일을 raw_dir에 해시 이름으로 복사해 보존한다. 같은 파일 재업로드 시 기존 id 반환."""
    src = Path(src)
    digest = file_sha256(src)
    row = conn.execute(
        "SELECT id FROM documents WHERE company_id=? AND sha256=?", (company_id, digest)
    ).fetchone()
    if row:
        return row["id"]
    dest = Path(raw_dir) / f"{digest}{src.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
    with conn:
        doc_id = conn.execute(
            "INSERT INTO documents(company_id, file_name, sha256, stored_path) VALUES (?,?,?,?)",
            (company_id, src.name, digest, str(dest)),
        ).lastrowid
        log(conn, "upload", "documents", doc_id, {"file_name": src.name, "sha256": digest})
    return doc_id


STATEMENT_COLS = (
    "company_id", "document_id", "fiscal_year", "fiscal_end_date", "scope", "account_code",
    "amount_krw", "raw_account_name", "raw_amount", "raw_unit", "source_row",
)


def insert_statements(conn, rows: list[dict]) -> int:
    with conn:
        conn.executemany(
            f"INSERT INTO financial_statements({','.join(STATEMENT_COLS)}) "
            f"VALUES ({','.join('?' * len(STATEMENT_COLS))})",
            [tuple(r[c] for c in STATEMENT_COLS) for r in rows],
        )
    return len(rows)


def get_statements(conn, document_id: int, scope: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM financial_statements WHERE document_id=? AND scope=? "
        "ORDER BY fiscal_year, account_code",
        (document_id, scope),
    ).fetchall()


def create_run(conn, case_id: int, document_id: int, scope: str, input_hash: str,
               config_hashes: dict, app_version: str, results: list[dict]) -> int:
    """run과 결과를 한 트랜잭션으로 저장한다 (일부만 저장되는 경우 방지)."""
    with conn:
        run_id = conn.execute(
            "INSERT INTO analysis_runs(case_id, document_id, scope, input_hash, config_hashes, app_version) "
            "VALUES (?,?,?,?,?,?)",
            (case_id, document_id, scope, input_hash, _json(config_hashes), app_version),
        ).lastrowid
        conn.executemany(
            "INSERT INTO analysis_results(run_id, kind, key, fiscal_year, value, status, payload_json) "
            "VALUES (?,?,?,?,?,?,?)",
            [(run_id, r["kind"], r["key"], r.get("fiscal_year"), r.get("value"), r["status"],
              _json(r.get("payload", {}))) for r in results],
        )
        log(conn, "create", "analysis_runs", run_id, {"results": len(results)})
    return run_id


def get_run(conn, run_id: int) -> dict | None:
    run = conn.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        return None
    results = conn.execute(
        "SELECT kind, key, fiscal_year, value, status, payload_json FROM analysis_results "
        "WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    out = dict(run)
    out["config_hashes"] = json.loads(out["config_hashes"])
    out["results"] = [
        {**{k: r[k] for k in ("kind", "key", "fiscal_year", "value", "status")},
         "payload": json.loads(r["payload_json"])}
        for r in results
    ]
    return out


def list_runs(conn, case_id: int | None = None) -> list[sqlite3.Row]:
    if case_id is None:
        return conn.execute("SELECT * FROM analysis_runs ORDER BY id DESC").fetchall()
    return conn.execute(
        "SELECT * FROM analysis_runs WHERE case_id=? ORDER BY id DESC", (case_id,)
    ).fetchall()
