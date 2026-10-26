import pytest

from cif.analysis.diagnosis import Peer, diagnose
from cif.analysis.ratios import compute_ratios
from cif.analysis.risk_rules import evaluate_risks
from cif.config import load_config
from cif.db import repository as repo
from cif.pipeline import find_peers, load_run, run_analysis, upload_statements
from cif.report.generator import generate_report
from cif.report.render import render_html
from cif.report.schema import CreditReport

T = 1_000_000_000_000  # 1조원


def year(ocf, ni=10 * T, adj=20 * T, wc=None, fin=None, **kw):
    base = dict(TOTAL_ASSETS=300 * T, CURRENT_ASSETS=120 * T, INVENTORIES=20 * T, TOTAL_LIABILITIES=180 * T,
                CURRENT_LIABILITIES=80 * T, TOTAL_EQUITY=120 * T, PAID_IN_CAPITAL=2 * T, REVENUE=170 * T,
                OPERATING_INCOME=14 * T, NET_INCOME=ni, INTEREST_EXPENSE=1 * T, OPERATING_CASH_FLOW=ocf)
    if wc is not None:
        base.update(CFO_ADJUSTMENTS=adj, CFO_WORKING_CAPITAL=wc)
    if fin is not None:
        base["FINANCING_CASH_FLOW"] = fin
    return {**base, **kw}


# 대상: 2년 연속 영업현금흐름 적자. 2024: OCF -5.6 = NI 13.2 + 조정 24.0 + 운전자본 -35.2 + 기타 -7.6
TARGET = {2023: year(-2 * T), 2024: year(-5.6e12, ni=13.2e12, adj=24e12, wc=-35.2e12, fin=19.5e12)}
HEALTHY = {2023: year(10 * T), 2024: year(12 * T)}
SAME_PROBLEM = {2023: year(-1 * T), 2024: year(-3 * T)}


@pytest.fixture(scope="module")
def rules():
    return load_config().risk


def _diag(values, rules, peers=None, rule_id="NEGATIVE_OPERATING_CASH_FLOW"):
    risks = evaluate_risks(values, compute_ratios(values), [], rules)
    return next(d for d in diagnose(values, risks, rules, peers) if d.rule_id == rule_id)


def peer(code, values):
    return Peer(code=code, name=f"비교{code}", values=values)


@pytest.mark.parametrize("peers,expected", [
    ([], "UNDETERMINED"),
    ([peer("A", HEALTHY)], "COMPANY_SPECIFIC"),
    ([peer("A", SAME_PROBLEM)], "INDUSTRY_COMMON"),
    ([peer("A", SAME_PROBLEM), peer("B", HEALTHY)], "INDUSTRY_COMMON"),   # 경계: 1/2 = 0.5 ≥ 기준 0.5
    ([peer("A", SAME_PROBLEM), peer("B", HEALTHY), peer("C", HEALTHY)], "COMPANY_SPECIFIC"),  # 1/3
    ([peer("A", {2022: year(1 * T)})], "UNDETERMINED"),                   # 같은 해 자료 없음
])
def test_peer_classification(rules, peers, expected):
    d = _diag(TARGET, rules, peers)
    assert d.classification == expected
    if peers and expected != "UNDETERMINED":
        assert f"{len(peers)}곳 중" in d.peer_summary


def test_peer_threshold_from_config(rules):
    strict = rules.model_copy(deep=True)
    strict.diagnosis.peer_common_share = 0.6
    d = _diag(TARGET, strict, [peer("A", SAME_PROBLEM), peer("B", HEALTHY)])
    assert d.classification == "COMPANY_SPECIFIC"


def test_ocf_decomposition_is_exact(rules):
    d = _diag(TARGET, rules)
    parts = {x.key: x.value for x in d.drivers}
    assert parts["net_income"] + parts["adjustments"] + parts["working_capital"] + parts["other"] == pytest.approx(-5.6e12)
    assert parts["other"] == pytest.approx(-7.6e12)
    assert "가장 큰 감소 요인은 영업 자산·부채 변동(-35.20조원)" in d.driver_summary
    assert "재무활동으로 19.50조원을 조달해 부족분을 메웠습니다" in d.driver_summary
    assert "금융업 채권" in d.estimate_note  # 단정하지 않고 확인 필요로 표시


def test_ocf_without_cash_flow_detail(rules):
    d = _diag({2023: year(-2 * T), 2024: year(-5 * T)}, rules)
    assert [x.key for x in d.drivers] == ["ni_minus_ocf"] and d.drivers[0].value == 15 * T
    assert "세부 항목" in d.driver_summary and d.estimate_note


def test_operating_loss_bridge_is_exact(rules):
    v = {2022: year(1 * T, REVENUE=100 * T, OPERATING_INCOME=5 * T),
         2023: year(1 * T, REVENUE=90 * T, OPERATING_INCOME=-1 * T),
         2024: year(1 * T, REVENUE=80 * T, OPERATING_INCOME=-4 * T)}
    d = _diag(v, rules, rule_id="OPERATING_LOSS_PERSISTENT")
    p = {x.key: x.value for x in d.drivers}
    assert p["revenue_effect"] + p["margin_effect"] == pytest.approx(-9 * T)   # -4 - 5
    assert p["revenue_effect"] == pytest.approx(-1 * T)                        # (80-100) × 5%
    assert p["revenue_change_pct"] == pytest.approx(-20) and p["margin_change_pctp"] == pytest.approx(-10)
    assert "이익률 효과" in d.driver_summary.split("더 큰 부분은")[1]


def test_liquidity_and_capital_drivers(rules):
    v = {2023: year(1 * T, CURRENT_ASSETS=120 * T, CURRENT_LIABILITIES=80 * T),
         2024: year(1 * T, CURRENT_ASSETS=70 * T, CURRENT_LIABILITIES=90 * T, TOTAL_EQUITY=-1 * T, NET_INCOME=-50 * T)}
    liq = _diag(v, rules, rule_id="LIQUIDITY_DETERIORATION")
    assert {x.key: x.value for x in liq.drivers} == {"ca_change": -50 * T, "cl_change": 10 * T}
    cap = _diag(v, rules, rule_id="CAPITAL_IMPAIRMENT_FULL")
    assert cap.drivers[0].value == -40 * T and "누적 당기순이익" in cap.driver_summary


def test_templates_from_config(rules):
    d = _diag(TARGET, rules)
    assert d.remediation == rules.rules["NEGATIVE_OPERATING_CASH_FLOW"].remediation and d.monitoring


def test_report_section_9_and_verified_opinions(report_inputs):
    inp = report_inputs("DEMO02")
    peers = [peer("P1", inp["validation"].values())]  # 같은 문제가 있는 비교 기업 → 산업 공통
    r = generate_report(**inp, peers=peers)
    assert r.diagnoses and all(d.classification in ("INDUSTRY_COMMON", "UNDETERMINED") for d in r.diagnoses)
    bad = [(c.text, c.reason) for c in r.overall_opinion if c.status != "VERIFIED"]
    assert bad == []
    assert any("산업 공통 요인" in c.text for c in r.overall_opinion)
    assert r.provenance.peer_inputs == {"비교P1": ""}
    assert any(e.startswith("diag:") for e in r.provenance.evidence_ids)
    html = render_html(r)
    for title in ("9-1. 위험 신호 원인 진단", "9-2. 개선 계획 및 확인 과제", "9-3. 종합 의견"):
        assert title in html


def test_old_reports_without_diagnoses_still_load(report_inputs):
    data = generate_report(**report_inputs("DEMO01")).model_dump(mode="json")
    data.pop("diagnoses")
    data["provenance"].pop("peer_inputs")
    old = CreditReport.model_validate(data)
    assert old.diagnoses == [] and "9-1" not in render_html(old)


def test_find_peers_same_industry_and_scope(conn, sample_dir, tmp_path):
    cfg = load_config()
    cfg.settings = cfg.settings.model_copy(update={"raw_dir": str(tmp_path / "raw")})
    ids = {}
    for code, industry in (("DEMO01", "자동차"), ("DEMO02", "자동차"), ("DEMO03", "바이오")):
        ids[code] = repo.create_company(conn, code, code, industry)
        upload_statements(conn, cfg, ids[code], sample_dir / f"{code}.csv")
    peers = find_peers(conn, cfg, ids["DEMO02"], "자동차", "CFS")
    assert [p.code for p in peers] == ["DEMO01"]
    assert find_peers(conn, cfg, ids["DEMO02"], "자동차", "OFS") == []
    assert find_peers(conn, cfg, ids["DEMO02"], None, "CFS") == []
    case_id = repo.create_case(conn, ids["DEMO02"], "C-PEER")
    doc = conn.execute("SELECT id FROM documents WHERE company_id=?", (ids["DEMO02"],)).fetchone()["id"]
    report = load_run(conn, run_analysis(conn, cfg, case_id, doc)).report
    assert list(report.provenance.peer_inputs) == ["DEMO01"]
    assert any(d.classification == "COMPANY_SPECIFIC" for d in report.diagnoses)  # DEMO01은 정상 기업
