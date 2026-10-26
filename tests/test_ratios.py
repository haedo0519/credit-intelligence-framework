import pytest

from cif.analysis.ratios import SPECS, compute_ratios

# 손으로 계산한 정답용 고정 데이터 (원)
BASE = {
    2024: dict(TOTAL_ASSETS=1000, CURRENT_ASSETS=400, INVENTORIES=100, TOTAL_LIABILITIES=600,
               CURRENT_LIABILITIES=200, TOTAL_EQUITY=400, PAID_IN_CAPITAL=100, REVENUE=2000,
               OPERATING_INCOME=150, NET_INCOME=80, INTEREST_EXPENSE=50, OPERATING_CASH_FLOW=120),
    2025: dict(TOTAL_ASSETS=1200, CURRENT_ASSETS=450, INVENTORIES=150, TOTAL_LIABILITIES=700,
               CURRENT_LIABILITIES=300, TOTAL_EQUITY=500, PAID_IN_CAPITAL=100, REVENUE=2400,
               OPERATING_INCOME=180, NET_INCOME=100, INTEREST_EXPENSE=60, OPERATING_CASH_FLOW=90),
}

EXPECTED_2025 = {
    "CURRENT_RATIO": 150.0,             # 450/300
    "QUICK_RATIO": 100.0,               # (450-150)/300
    "DEBT_RATIO": 140.0,                # 700/500
    "EQUITY_RATIO": 41.666666666666664, # 500/1200
    "OPERATING_MARGIN": 7.5,            # 180/2400
    "ROA": 9.090909090909092,           # 100/((1000+1200)/2)
    "ROE": 22.22222222222222,           # 100/((400+500)/2)
    "ASSET_TURNOVER": 2.1818181818181817,  # 2400/1100
    "INTEREST_COVERAGE": 3.0,           # 180/60
    "OCF_RATIO": 30.0,                  # 90/300
}


def _by(results, year):
    return {r.code: r for r in results if r.fiscal_year == year}


def _with(year_values, **changes):
    return {2025: {**year_values, **changes}}


def test_matches_hand_calculation():
    got = _by(compute_ratios(BASE), 2025)
    assert set(got) == set(EXPECTED_2025)
    for code, expected in EXPECTED_2025.items():
        assert got[code].status == "OK", code
        assert got[code].value == pytest.approx(expected, rel=1e-12), code
    assert got["ROA"].basis == "AVERAGE" and got["ROA"].inputs["TOTAL_ASSETS_PRIOR"] == 1000


def test_first_year_falls_back_to_end_balance():
    got = _by(compute_ratios(BASE), 2024)
    assert got["ROA"].value == pytest.approx(8.0)          # 80/1000
    assert got["ROE"].value == pytest.approx(20.0)         # 80/400
    assert got["ASSET_TURNOVER"].value == pytest.approx(2.0)
    assert got["ROA"].basis == "END" and "기말잔액" in got["ROA"].note


def test_value_none_unless_ok():
    data = {2025: {**BASE[2025], "TOTAL_EQUITY": -10, "CURRENT_LIABILITIES": 0, "INVENTORIES": None}}
    data[2025] = {k: v for k, v in data[2025].items() if v is not None}
    for r in compute_ratios(data):
        assert (r.value is not None) == (r.status == "OK"), r.code


@pytest.mark.parametrize("account,codes", [
    ("CURRENT_LIABILITIES", ["CURRENT_RATIO", "QUICK_RATIO", "OCF_RATIO"]),
    ("TOTAL_ASSETS", ["EQUITY_RATIO", "ROA", "ASSET_TURNOVER"]),
    ("REVENUE", ["OPERATING_MARGIN"]),
])
def test_zero_denominator(account, codes):
    got = _by(compute_ratios(_with(BASE[2025], **{account: 0})), 2025)
    for c in codes:
        assert got[c].status == "DIV_BY_ZERO" and got[c].value is None, c


def test_zero_interest_is_not_a_risk_status():
    got = _by(compute_ratios(_with(BASE[2025], INTEREST_EXPENSE=0)), 2025)
    assert got["INTEREST_COVERAGE"].status == "NO_INTEREST_EXPENSE"


@pytest.mark.parametrize("equity", [0, -300])
def test_non_positive_equity(equity):
    got = _by(compute_ratios(_with(BASE[2025], TOTAL_EQUITY=equity)), 2025)
    assert got["DEBT_RATIO"].status == "NOT_MEANINGFUL"
    assert got["ROE"].status == "NOT_MEANINGFUL"
    if equity < 0:  # 자기자본비율은 음수도 의미가 있어 계산한다
        assert got["EQUITY_RATIO"].value == pytest.approx(-25.0)


def test_roe_not_meaningful_when_prior_equity_negative():
    data = {2024: {**BASE[2024], "TOTAL_EQUITY": -100}, 2025: BASE[2025]}
    # 평균(200)은 양수지만 전기 자본이 음수라 ROE 해석 불가
    assert _by(compute_ratios(data), 2025)["ROE"].status == "NOT_MEANINGFUL"


def test_negative_operating_income():
    got = _by(compute_ratios(_with(BASE[2025], OPERATING_INCOME=-240)), 2025)
    assert got["OPERATING_MARGIN"].value == pytest.approx(-10.0)
    assert got["INTEREST_COVERAGE"].value == pytest.approx(-4.0)


def test_missing_input():
    data = {2025: {k: v for k, v in BASE[2025].items() if k != "INVENTORIES"}}
    got = _by(compute_ratios(data), 2025)
    assert got["QUICK_RATIO"].status == "MISSING_INPUT"
    assert "INVENTORIES" in got["QUICK_RATIO"].note
    assert got["CURRENT_RATIO"].status == "OK"  # 다른 비율은 영향 없음


def test_negative_denominator_not_meaningful():
    got = _by(compute_ratios(_with(BASE[2025], INTEREST_EXPENSE=-60)), 2025)
    assert got["INTEREST_COVERAGE"].status == "NOT_MEANINGFUL"


def test_deterministic_and_ordered():
    a, b = compute_ratios(BASE), compute_ratios(BASE)
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]
    assert [r.code for r in a[:len(SPECS)]] == [s[0] for s in SPECS]
    assert a[0].evidence_id == "ratio:CURRENT_RATIO:2024"
