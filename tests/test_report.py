import pytest

from cif.db import repository as repo
from cif.report.evidence import DECISION_PATTERNS
from cif.report.generator import generate_report
from cif.report.schema import CreditReport
from cif.sample_data import COMPANIES


def _all_claims(r: CreditReport):
    return (r.trend_section.commentary + r.ratio_section.commentary + r.positives + r.risk_section.commentary
            + r.stress_section.commentary + r.overall_opinion + r.ai_limitations)


@pytest.mark.parametrize("code", list(COMPANIES))
def test_rule_based_report_fully_verified(report_inputs, code):
    r = generate_report(**report_inputs(code))
    assert r.provenance.ai_mode == "RULE_BASED_FALLBACK" and r.provenance.ai_model is None
    bad = [(c.text, c.reason) for c in _all_claims(r) if c.status != "VERIFIED"]
    assert bad == []  # 규칙 기반 문장은 근거 값을 옮기므로 전부 검증을 통과해야 한다
    assert r.overall_opinion


def test_ten_sections_and_provenance(report_inputs):
    inp = report_inputs("DEMO02")
    r = generate_report(**inp)
    assert r.overview.case_no == "CASE-DEMO02" and r.overview.fiscal_years == [2023, 2024, 2025]  # 1
    assert any("업종" in s for s in r.limitations)                                                # 2
    assert {t.code for t in r.trend} >= {"REVENUE", "OPERATING_INCOME"}                            # 3
    assert len(r.ratios) == 10                                                                     # 4
    assert r.positives                                                                             # 5
    assert {e.rule_id for e in r.risk_signals} >= {"OPERATING_LOSS_PERSISTENT"}                    # 6
    assert [s.scenario_id for s in r.stress][0] == "BASE" and r.stress_section.commentary          # 7
    assert isinstance(r.additional_documents, list)                                                # 8
    assert "승인·거절을 결정하지 않습니다" in r.disclaimer                                           # 9
    p = r.provenance                                                                               # 10
    assert p.config_hashes == inp["cfg"].hashes and p.input_hash == inp["input_hash"]
    assert p.rule_version == inp["cfg"].risk.version and p.prompt_version
    assert "ratio:DEBT_RATIO:2025" in p.evidence_ids


def test_non_ok_ratios_not_reported_as_numbers(report_inputs):
    r = generate_report(**report_inputs("DEMO03"))
    debt = next(t for t in r.ratios if t.code == "DEBT_RATIO")
    assert debt.cells[2025] is None and debt.statuses[2025] == "NOT_MEANINGFUL"


def test_dirty_data_produces_document_requests(report_inputs):
    r = generate_report(**report_inputs("DEMO04"))
    items = [d.item for d in r.additional_documents if d.source == "RULE"]
    assert any("재고자산" in i for i in items)
    assert any("연결" in i for i in items)
    assert any("SCOPE_MIXED" in s or "연결/별도" in s for s in r.limitations)


def test_equity_gap_becomes_document_request_not_risk(report_inputs):
    from cif.analysis.risk_rules import evaluate_risks
    from cif.ingest.mapping import Issue
    inp = report_inputs("DEMO01")
    gap = Issue(level="WARNING", code="EQUITY_ROLLFORWARD_GAP", message="자본 변동 차이", fiscal_year=2025,
                account_code="TOTAL_EQUITY")
    v = inp["validation"].model_copy(update={"issues": inp["validation"].issues + [gap]})
    risks = evaluate_risks(v.values(), inp["ratios"], v.issues, inp["cfg"].risk)
    assert next(e for e in risks if e.rule_id == "DATA_INCONSISTENCY").status == "NOT_TRIGGERED"
    r = generate_report(**{**inp, "validation": v, "risks": risks})
    assert any(d.item.startswith("자본변동표") and d.source == "RULE" for d in r.additional_documents)


def test_no_decision_language_anywhere(report_inputs):
    for code in COMPANIES:
        r = generate_report(**report_inputs(code))
        for c in _all_claims(r):
            assert not any(p.search(c.text) for p in DECISION_PATTERNS), c.text


def test_same_input_same_report(report_inputs):
    a = generate_report(**report_inputs("DEMO04")).model_dump()
    b = generate_report(**report_inputs("DEMO04")).model_dump()
    assert a == b


def test_invalid_validation_rejected(report_inputs):
    inp = report_inputs("DEMO01")
    inp["validation"] = inp["validation"].model_copy(update={"ok": False})
    with pytest.raises(ValueError):
        generate_report(**inp)


def test_report_saved_and_reloaded(report_inputs, conn, tmp_path, sample_dir):
    inp = report_inputs("DEMO02")
    r = generate_report(**inp)
    cid = repo.create_company(conn, "DEMO02", inp["company_name"])
    case_id = repo.create_case(conn, cid, inp["case_no"])
    doc_id = repo.store_document(conn, cid, sample_dir / "DEMO02.csv", tmp_path / "raw")
    run_id = repo.create_run(conn, case_id, doc_id, r.overview.scope, inp["input_hash"], inp["cfg"].hashes,
                             r.provenance.app_version,
                             [{"kind": "report", "key": "CREDIT_REPORT", "status": r.provenance.ai_mode,
                               "payload": r.model_dump(mode="json")}])
    saved = repo.get_run(conn, run_id)["results"][0]
    assert CreditReport.model_validate(saved["payload"]) == r
