import pytest

from cif.config import load_config
from cif.db import repository as repo
from cif.pipeline import (compare_runs, load_run, run_analysis, seed_demo, statement_values,
                          upload_statements)


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.settings = c.settings.model_copy(update={"raw_dir": str(tmp_path / "raw")})
    return c


def _case(conn, code="DEMO02"):
    cid = repo.create_company(conn, code, "테스트")
    return cid, repo.create_case(conn, cid, f"CASE-{code}")


def test_upload_and_run(conn, cfg, sample_dir):
    cid, case_id = _case(conn)
    up = upload_statements(conn, cfg, cid, sample_dir / "DEMO02.csv")
    assert up.validation.ok and up.stored_rows == len(up.validation.rows) > 0
    run_id = run_analysis(conn, cfg, case_id, up.document_id)
    run = load_run(conn, run_id)
    assert len(run.ratios) == 30 and run.risks and run.stress and run.report is not None
    assert run.report.provenance.input_hash == run.input_hash
    assert run.config_hashes == cfg.hashes
    assert statement_values(conn, up.document_id, "CFS") == up.validation.values()


def test_reupload_same_file_is_idempotent(conn, cfg, sample_dir):
    cid, _ = _case(conn)
    a = upload_statements(conn, cfg, cid, sample_dir / "DEMO02.csv")
    b = upload_statements(conn, cfg, cid, sample_dir / "DEMO02.csv")
    assert a.document_id == b.document_id and b.stored_rows == 0


def test_company_mismatch_not_stored(conn, cfg, sample_dir):
    cid, _ = _case(conn, "OTHER")
    up = upload_statements(conn, cfg, cid, sample_dir / "DEMO02.csv")
    assert not up.validation.ok and up.document_id is None
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_invalid_file_stored_but_not_analyzable(conn, cfg, tmp_path):
    cid, case_id = _case(conn, "T01")
    bad = tmp_path / "bad.csv"
    bad.write_text("company_code,fiscal_year,fiscal_end_date,statement_scope,account_name,amount,unit\n"
                   "T01,2025,2025-12-31,CFS,매출액,abc,원\n", encoding="utf-8")
    up = upload_statements(conn, cfg, cid, bad)
    assert not up.validation.ok and up.document_id is not None and up.stored_rows == 0  # 원본은 보존
    with pytest.raises(ValueError, match="검증 오류"):
        run_analysis(conn, cfg, case_id, up.document_id)


def test_document_case_mismatch(conn, cfg, sample_dir):
    cid, _ = _case(conn)
    _, other_case = _case(conn, "DEMO01")
    up = upload_statements(conn, cfg, cid, sample_dir / "DEMO02.csv")
    with pytest.raises(ValueError, match="기업이 다릅니다"):
        run_analysis(conn, cfg, other_case, up.document_id)


def test_rerun_same_input_same_rules_identical(conn, cfg, sample_dir):
    cid, case_id = _case(conn)
    up = upload_statements(conn, cfg, cid, sample_dir / "DEMO02.csv")
    r1 = load_run(conn, run_analysis(conn, cfg, case_id, up.document_id))
    r2 = load_run(conn, run_analysis(conn, cfg, case_id, up.document_id))
    diff = compare_runs(r1, r2)
    assert diff.identical_results and not diff.config_changed and not diff.input_changed
    assert diff.ratio_changes == [] and diff.risk_changes == []
    assert r1.report == r2.report
    assert len(repo.list_runs(conn, case_id)) == 2  # 덮어쓰지 않고 추가


def test_rule_change_detected(conn, cfg, sample_dir):
    cid, case_id = _case(conn, "DEMO01")
    up = upload_statements(conn, cfg, cid, sample_dir / "DEMO01.csv")
    r1 = load_run(conn, run_analysis(conn, cfg, case_id, up.document_id))
    stricter = cfg.model_copy(deep=True)
    stricter.risk.rules["LIQUIDITY_DETERIORATION"].params["min_current_ratio_pct"] = 200.0
    stricter.hashes["risk"] = "f" * 64  # 설정 파일이 바뀐 것과 같은 상황
    r2 = load_run(conn, run_analysis(conn, stricter, case_id, up.document_id))
    diff = compare_runs(r1, r2)
    assert "risk" in diff.config_changed and not diff.identical_results
    assert diff.risk_changes == [{"rule_id": "LIQUIDITY_DETERIORATION", "before": "NOT_TRIGGERED/-",
                                  "after": "TRIGGERED/MEDIUM"}]
    assert diff.ratio_changes == []  # 비율 계산은 규칙과 무관


def test_seed_demo(conn, cfg, sample_dir):
    ids = seed_demo(conn, cfg, sample_dir)
    assert len(ids) == 4
    assert seed_demo(conn, cfg, sample_dir) == []  # 두 번째는 건너뜀
    actions = {r[0] for r in conn.execute("SELECT action FROM audit_logs")}
    assert {"create", "upload", "validate"} <= actions
