import sqlite3

import pytest

from cif.db import repository as repo

TABLES = {"companies", "credit_cases", "financial_statements", "documents",
          "analysis_runs", "analysis_results", "audit_logs"}


def _setup(conn, tmp_path):
    cid = repo.create_company(conn, "T01", "테스트(주)")
    case_id = repo.create_case(conn, cid, "CASE-001")
    src = tmp_path / "in.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    doc_id = repo.store_document(conn, cid, src, tmp_path / "raw")
    return cid, case_id, doc_id


def _stmt(cid, doc_id, **kw):
    row = dict(company_id=cid, document_id=doc_id, fiscal_year=2025, fiscal_end_date="2025-12-31",
               scope="CFS", account_code="REVENUE", amount_krw=1_000_000, raw_account_name="매출액",
               raw_amount="1", raw_unit="백만원", source_row=2)
    return {**row, **kw}


RESULTS = [
    {"kind": "ratio", "key": "CURRENT_RATIO", "fiscal_year": 2025, "value": 150.0, "status": "OK",
     "payload": {"formula": "CA/CL*100"}},
    {"kind": "ratio", "key": "DEBT_RATIO", "fiscal_year": 2025, "value": None, "status": "NOT_MEANINGFUL"},
]


def test_schema_creates_all_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_connect_is_idempotent(tmp_path):
    repo.connect(tmp_path / "x.db").close()
    repo.connect(tmp_path / "x.db").close()  # IF NOT EXISTS: 두 번째 연결도 오류 없음


def test_foreign_key_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_case(conn, 999, "CASE-X")


def test_unique_company_code(conn):
    repo.create_company(conn, "T01", "A")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_company(conn, "T01", "B")


def test_statement_constraints(conn, tmp_path):
    cid, _, doc_id = _setup(conn, tmp_path)
    repo.insert_statements(conn, [_stmt(cid, doc_id)])
    with pytest.raises(sqlite3.IntegrityError):  # 같은 키 중복
        repo.insert_statements(conn, [_stmt(cid, doc_id)])
    with pytest.raises(sqlite3.IntegrityError):  # 범위 CHECK
        repo.insert_statements(conn, [_stmt(cid, doc_id, scope="XX", account_code="NET_INCOME")])
    assert len(repo.get_statements(conn, doc_id, "CFS")) == 1


def test_document_preserved_and_deduplicated(conn, tmp_path):
    cid, _, doc_id = _setup(conn, tmp_path)
    stored = conn.execute("SELECT stored_path, sha256 FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert open(stored["stored_path"], encoding="utf-8").read() == "a,b\n1,2\n"
    assert repo.store_document(conn, cid, tmp_path / "in.csv", tmp_path / "raw") == doc_id


def test_run_roundtrip(conn, tmp_path):
    _, case_id, doc_id = _setup(conn, tmp_path)
    hashes = {"settings": "a" * 64, "risk": "b" * 64}
    run_id = repo.create_run(conn, case_id, doc_id, "CFS", "c" * 64, hashes, "0.1.0", RESULTS)
    run = repo.get_run(conn, run_id)
    assert run["config_hashes"] == hashes
    assert [r["key"] for r in run["results"]] == ["CURRENT_RATIO", "DEBT_RATIO"]
    assert run["results"][0]["payload"] == {"formula": "CA/CL*100"}
    assert run["results"][1]["value"] is None
    assert repo.get_run(conn, 12345) is None


def test_rerun_appends_history(conn, tmp_path):
    _, case_id, doc_id = _setup(conn, tmp_path)
    r1 = repo.create_run(conn, case_id, doc_id, "CFS", "c" * 64, {}, "0.1.0", RESULTS)
    r2 = repo.create_run(conn, case_id, doc_id, "CFS", "c" * 64, {}, "0.1.0", RESULTS)
    assert [r["id"] for r in repo.list_runs(conn, case_id)] == [r2, r1]
    assert repo.get_run(conn, r1)["results"] == repo.get_run(conn, r2)["results"]


def test_run_is_atomic(conn, tmp_path):
    _, case_id, doc_id = _setup(conn, tmp_path)
    bad = RESULTS + [{"kind": "invalid_kind", "key": "X", "status": "OK"}]
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_run(conn, case_id, doc_id, "CFS", "c" * 64, {}, "0.1.0", bad)
    assert repo.list_runs(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0] == 0


@pytest.mark.parametrize("sql", [
    "UPDATE analysis_runs SET app_version='x'",
    "DELETE FROM analysis_runs",
    "UPDATE analysis_results SET value=0",
    "DELETE FROM analysis_results",
    "UPDATE audit_logs SET action='x'",
    "DELETE FROM audit_logs",
    "UPDATE documents SET file_name='x'",
    "UPDATE financial_statements SET amount_krw=0",
])
def test_history_cannot_be_modified(conn, tmp_path, sql):
    cid, case_id, doc_id = _setup(conn, tmp_path)
    repo.insert_statements(conn, [_stmt(cid, doc_id)])
    repo.create_run(conn, case_id, doc_id, "CFS", "c" * 64, {}, "0.1.0", RESULTS)
    with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
        conn.execute(sql)


def test_audit_log_written(conn, tmp_path):
    _setup(conn, tmp_path)
    actions = [(r["action"], r["entity"]) for r in conn.execute("SELECT action, entity FROM audit_logs")]
    assert actions == [("create", "companies"), ("create", "credit_cases"), ("upload", "documents")]
