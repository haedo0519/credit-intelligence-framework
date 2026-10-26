import pytest

from cif.analysis.ratios import compute_ratios
from cif.analysis.risk_rules import evaluate_risks
from cif.config import load_config
from cif.ingest.mapping import Issue
from cif.ingest.validation import validate_file
from cif.sample_data import write_samples


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    write_samples(d)
    return d


GOOD = dict(TOTAL_ASSETS=1000, CURRENT_ASSETS=400, INVENTORIES=100, TOTAL_LIABILITIES=600,
            CURRENT_LIABILITIES=200, TOTAL_EQUITY=400, PAID_IN_CAPITAL=100, REVENUE=2000,
            OPERATING_INCOME=150, NET_INCOME=80, INTEREST_EXPENSE=50, OPERATING_CASH_FLOW=120)


def _eval(cfg, values, issues=(), rules=None):
    ev = evaluate_risks(values, compute_ratios(values), list(issues), rules or cfg.risk)
    return {e.rule_id: e for e in ev}


def _one(**changes):
    return {2025: {**GOOD, **changes}}


def _two(prev=None, **changes):
    return {2024: {**GOOD, **(prev or {})}, 2025: {**GOOD, **changes}}


def test_all_rules_evaluated_and_versioned(cfg):
    got = _eval(cfg, _two())
    assert set(got) == set(cfg.risk.rules)
    assert all(e.status == "NOT_TRIGGERED" and e.severity is None for e in got.values())
    assert {e.rule_version for e in got.values()} == {cfg.risk.version}


def test_empty_values():
    assert evaluate_risks({}, [], [], load_config().risk) == []


@pytest.mark.parametrize("equity,full,partial", [
    (-1, "TRIGGERED", "NOT_TRIGGERED"),
    (0, "TRIGGERED", "NOT_TRIGGERED"),      # 경계: 0은 완전잠식
    (1, "NOT_TRIGGERED", "TRIGGERED"),
    (99, "NOT_TRIGGERED", "TRIGGERED"),
    (100, "NOT_TRIGGERED", "NOT_TRIGGERED"),  # 경계: 자본금과 같으면 잠식 아님
])
def test_capital_impairment_boundaries(cfg, equity, full, partial):
    got = _eval(cfg, _one(TOTAL_EQUITY=equity))
    assert got["CAPITAL_IMPAIRMENT_FULL"].status == full
    assert got["CAPITAL_IMPAIRMENT_PARTIAL"].status == partial


def test_capital_missing_inputs(cfg):
    v = _one()
    del v[2025]["PAID_IN_CAPITAL"]
    assert _eval(cfg, v)["CAPITAL_IMPAIRMENT_PARTIAL"].status == "NOT_EVALUATED"
    del v[2025]["TOTAL_EQUITY"]
    assert _eval(cfg, v)["CAPITAL_IMPAIRMENT_FULL"].status == "NOT_EVALUATED"


def test_operating_loss(cfg):
    assert _eval(cfg, _two(prev=dict(OPERATING_INCOME=-1), OPERATING_INCOME=-1))[
        "OPERATING_LOSS_PERSISTENT"].status == "TRIGGERED"
    assert _eval(cfg, _two(OPERATING_INCOME=-1))["OPERATING_LOSS_PERSISTENT"].status == "NOT_TRIGGERED"
    assert _eval(cfg, _two(prev=dict(OPERATING_INCOME=0), OPERATING_INCOME=-1))[
        "OPERATING_LOSS_PERSISTENT"].status == "NOT_TRIGGERED"  # 경계: 0은 손실 아님
    # 1개년 자료만 있으면 연속 여부를 판단할 수 없다
    assert _eval(cfg, _one(OPERATING_INCOME=-1))["OPERATING_LOSS_PERSISTENT"].status == "NOT_EVALUATED"


@pytest.mark.parametrize("oi,status", [(50, "NOT_TRIGGERED"), (49, "TRIGGERED"), (-10, "TRIGGERED")])
def test_interest_coverage_min_boundary(cfg, oi, status):
    # 이자비용 50: 영업이익 50 → 1.0배 (기준 1.0 미만 아님)
    assert _eval(cfg, _one(OPERATING_INCOME=oi))["INTEREST_COVERAGE_WEAK"].status == status


def test_interest_coverage_drop_boundary(cfg):
    # 전년 3.0배(150/50) → 당해 1.0배(50/50): 하락폭 2.0 = 기준 → 신호
    got = _eval(cfg, _two(OPERATING_INCOME=50))["INTEREST_COVERAGE_WEAK"]
    assert got.status == "TRIGGERED" and "하락" in got.message
    assert _eval(cfg, _two(OPERATING_INCOME=51))["INTEREST_COVERAGE_WEAK"].status == "NOT_TRIGGERED"


def test_no_interest_expense_is_not_risk(cfg):
    assert _eval(cfg, _one(INTEREST_EXPENSE=0))["INTEREST_COVERAGE_WEAK"].status == "NOT_TRIGGERED"
    v = _one()
    del v[2025]["INTEREST_EXPENSE"]
    assert _eval(cfg, v)["INTEREST_COVERAGE_WEAK"].status == "NOT_EVALUATED"


@pytest.mark.parametrize("ca,status", [(200, "NOT_TRIGGERED"), (199, "TRIGGERED")])
def test_liquidity_min_boundary(cfg, ca, status):
    assert _eval(cfg, _one(CURRENT_ASSETS=ca, INVENTORIES=0))["LIQUIDITY_DETERIORATION"].status == status


def test_liquidity_drop_boundary(cfg):
    # 전년 200% → 당해 180%: 20%p 하락 = 기준 → 신호
    assert _eval(cfg, _two(CURRENT_ASSETS=360))["LIQUIDITY_DETERIORATION"].status == "TRIGGERED"
    assert _eval(cfg, _two(CURRENT_ASSETS=362))["LIQUIDITY_DETERIORATION"].status == "NOT_TRIGGERED"


def test_negative_ocf_and_escalation(cfg):
    one = _eval(cfg, _two(OPERATING_CASH_FLOW=-1))["NEGATIVE_OPERATING_CASH_FLOW"]
    assert one.status == "TRIGGERED" and one.severity == "MEDIUM"
    two = _eval(cfg, _two(prev=dict(OPERATING_CASH_FLOW=-5), OPERATING_CASH_FLOW=-1))["NEGATIVE_OPERATING_CASH_FLOW"]
    assert two.severity == "HIGH" and len(two.evidence_ids) == 2
    assert _eval(cfg, _two(OPERATING_CASH_FLOW=0))["NEGATIVE_OPERATING_CASH_FLOW"].status == "NOT_TRIGGERED"


def test_data_inconsistency(cfg):
    issues = [Issue(level="WARNING", code="BALANCE_MISMATCH", message="x", fiscal_year=2025),
              Issue(level="INFO", code="UNIT_MIXED", message="단위 혼합은 불일치가 아님")]
    got = _eval(cfg, _one(), issues)["DATA_INCONSISTENCY"]
    assert got.status == "TRIGGERED" and got.evidence_ids == ["issue:BALANCE_MISMATCH:2025"]


def test_thresholds_come_from_config(cfg):
    stricter = cfg.risk.model_copy(deep=True)
    stricter.rules["LIQUIDITY_DETERIORATION"].params["min_current_ratio_pct"] = 250.0
    stricter.version = "test-2"
    got = _eval(cfg, _one(), rules=stricter)["LIQUIDITY_DETERIORATION"]
    assert got.status == "TRIGGERED" and got.rule_version == "test-2"


def test_triggered_signals_have_evidence(cfg, samples):
    for code in ("DEMO02", "DEMO03", "DEMO04"):
        r = validate_file(samples / f"{code}.csv", cfg)
        for e in evaluate_risks(r.values(), compute_ratios(r.values()), r.issues, cfg.risk):
            if e.status == "TRIGGERED":
                assert e.evidence_ids and e.severity, (code, e.rule_id)


def _triggered(cfg, samples, code):
    r = validate_file(samples / f"{code}.csv", cfg)
    return {e.rule_id: e.severity for e in evaluate_risks(r.values(), compute_ratios(r.values()), r.issues, cfg.risk)
            if e.status == "TRIGGERED"}


def test_sample_profiles(cfg, samples):
    assert _triggered(cfg, samples, "DEMO01") == {}
    assert _triggered(cfg, samples, "DEMO02") == {
        "OPERATING_LOSS_PERSISTENT": "HIGH", "INTEREST_COVERAGE_WEAK": "HIGH",
        "NEGATIVE_OPERATING_CASH_FLOW": "HIGH", "LIQUIDITY_DETERIORATION": "MEDIUM"}
    demo03 = _triggered(cfg, samples, "DEMO03")
    assert demo03["CAPITAL_IMPAIRMENT_FULL"] == "HIGH" and "CAPITAL_IMPAIRMENT_PARTIAL" not in demo03
    assert "DATA_INCONSISTENCY" in _triggered(cfg, samples, "DEMO04")
