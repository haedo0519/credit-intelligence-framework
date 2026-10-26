import pytest

from cif.analysis.ratios import compute_ratios
from cif.analysis.risk_rules import evaluate_risks
from cif.analysis.stress import run_stress
from cif.config import load_config
from cif.ingest.mapping import Issue
from cif.report.evidence import Claim, build_evidence, verify_claim

VALUES = {
    2024: dict(TOTAL_ASSETS=1_000_000_000, CURRENT_ASSETS=400_000_000, INVENTORIES=100_000_000,
               TOTAL_LIABILITIES=600_000_000, CURRENT_LIABILITIES=200_000_000, TOTAL_EQUITY=400_000_000,
               PAID_IN_CAPITAL=100_000_000, REVENUE=2_000_000_000, OPERATING_INCOME=150_000_000,
               NET_INCOME=80_000_000, INTEREST_EXPENSE=50_000_000, OPERATING_CASH_FLOW=120_000_000,
               BORROWINGS=300_000_000),
    2025: dict(TOTAL_ASSETS=1_200_000_000, CURRENT_ASSETS=360_000_000, INVENTORIES=150_000_000,
               TOTAL_LIABILITIES=700_000_000, CURRENT_LIABILITIES=200_000_000, TOTAL_EQUITY=500_000_000,
               PAID_IN_CAPITAL=100_000_000, REVENUE=2_400_000_000, OPERATING_INCOME=-210_000_000,
               NET_INCOME=100_000_000, INTEREST_EXPENSE=60_000_000, OPERATING_CASH_FLOW=90_000_000,
               BORROWINGS=300_000_000),
}


@pytest.fixture(scope="module")
def pack():
    cfg = load_config()
    ratios = compute_ratios(VALUES)
    issues = [Issue(level="WARNING", code="BALANCE_MISMATCH", message="불일치", fiscal_year=2025)]
    risks = evaluate_risks(VALUES, ratios, issues, cfg.risk)
    labels = {c: n[0] for c, n in cfg.accounts.accounts.items()}
    return build_evidence(VALUES, ratios, risks, run_stress(VALUES, cfg.stress), issues, cfg.risk, labels)


def _status(pack, text, *ids):
    return verify_claim(Claim(text=text, evidence_ids=list(ids)), pack).status


REV24, REV25 = "account:REVENUE:2024", "account:REVENUE:2025"


def test_pack_contents(pack):
    assert pack[REV25].value == 2_400_000_000 and pack[REV25].label == "매출액"
    assert pack["ratio:CURRENT_RATIO:2025"].value == pytest.approx(180.0)
    assert pack["risk:OPERATING_LOSS_PERSISTENT"].status == "NOT_TRIGGERED"
    assert "ratio:INTEREST_COVERAGE:2025" in pack["risk:INTEREST_COVERAGE_WEAK"].related
    assert pack["issue:BALANCE_MISMATCH:2025"].kind == "issue"
    assert pack["stress:COMBINED_SEVERE:rate_change_bp"].value == 200


@pytest.mark.parametrize("text", [
    "2025년 매출액은 2,400,000,000원입니다.",
    "2025년 매출액은 2,400백만원입니다.",
    "2025년 매출액은 24억원입니다.",
    "2025년 매출액은 약 24.0억원입니다.",
    "매출액은 2,000백만원에서 2,400백만원으로 증가했습니다.",
    "매출액이 전년 대비 400백만원 늘었습니다.",       # 차이
    "매출액이 전년 대비 20% 증가했습니다.",           # 증감률
    "3개년 중 2025년 기준입니다.",                    # 연도·개수는 검증 대상 아님
    "2024~2025년 매출액은 2,000백만원, 2,400백만원입니다.",  # 연도 범위 (실제 API 응답에서 발견)
    "2024 ~ 2025년 기준입니다.",
])
def test_verified_amounts(pack, text):
    assert _status(pack, text, REV24, REV25) == "VERIFIED"


@pytest.mark.parametrize("text", [
    "2025년 매출액은 2,500백만원입니다.",
    "2025년 매출액은 25억원입니다.",
    "매출액이 전년 대비 25% 증가했습니다.",
    "매출액은 2,401백만원입니다.",
    "매출액은 2024~2,500백만원입니다.",               # 연도 범위가 아니면 숫자로 검사
])
def test_wrong_amounts_rejected(pack, text):
    assert _status(pack, text, REV24, REV25) == "UNVERIFIED_NUMBER"


def test_jo_won_unit():
    """대기업 금액(조원) 표기 검증 (실제 기업 현대자동차 보고서에서 필요해짐)."""
    cfg = load_config()
    big = {2025: {"REVENUE": 175_231_153_000_000}}
    p = build_evidence(big, [], [], [], [], cfg.risk, {"REVENUE": "매출액"})
    assert verify_claim(Claim(text="매출액은 175.23조원입니다.", evidence_ids=["account:REVENUE:2025"]), p).status == "VERIFIED"
    assert verify_claim(Claim(text="매출액은 175.3조원입니다.", evidence_ids=["account:REVENUE:2025"]),
                        p).status == "UNVERIFIED_NUMBER"


def test_ratio_values_and_differences(pack):
    ids = ("ratio:CURRENT_RATIO:2024", "ratio:CURRENT_RATIO:2025")
    assert _status(pack, "유동비율은 200.0%에서 180.0%로 20.0%p 하락했습니다.", *ids) == "VERIFIED"
    assert _status(pack, "유동비율은 181%입니다.", *ids) == "UNVERIFIED_NUMBER"
    assert _status(pack, "유동비율은 180%입니다.", *ids) == "VERIFIED"


def test_negative_values_by_magnitude(pack):
    assert _status(pack, "2025년 영업손실 −210백만원", "account:OPERATING_INCOME:2025") == "VERIFIED"
    assert _status(pack, "이자보상배율 -3.50배", "ratio:INTEREST_COVERAGE:2025") == "VERIFIED"


def test_number_must_come_from_cited_evidence(pack):
    # 값은 패키지에 있지만 인용하지 않은 근거의 값
    assert _status(pack, "매출액은 2,400백만원입니다.", "account:TOTAL_ASSETS:2025") == "UNVERIFIED_NUMBER"


def test_risk_citation_includes_rule_params_and_inputs(pack):
    text = "이자보상배율이 기준 1.0배에 못 미치는 -3.50배입니다."
    assert _status(pack, text, "risk:INTEREST_COVERAGE_WEAK") == "VERIFIED"


def test_stress_scenario_params(pack):
    ids = [f"stress:COMBINED_SEVERE:{f}" for f in ("revenue_change_pct", "op_margin_change_pctp", "rate_change_bp")]
    assert _status(pack, "매출 -20%, 이익률 -3%p, 금리 +200bp 복합 시나리오", *ids) == "VERIFIED"


@pytest.mark.parametrize("ids", [(), ("account:NOPE:2025",)])
def test_unsupported(pack, ids):
    assert _status(pack, "근거 없는 주장", *ids) == "UNSUPPORTED"


@pytest.mark.parametrize("text", [
    "본 건 대출을 승인합니다.",
    "여신 거절을 권고합니다.",
    "대출 가능하다고 판단됩니다.",
    "승인을 권고합니다.",
    "부결하는 것이 바람직합니다.",
    "We recommend approval.",
])
def test_decision_language_blocked(pack, text):
    assert _status(pack, text, REV25) == "POLICY_VIOLATION"


def test_ordinary_approval_words_allowed(pack):
    assert _status(pack, "차입 관련 이사회 승인 의사록 확인이 필요합니다.", REV25) == "VERIFIED"
