import pytest

from cif.analysis.stress import run_stress
from cif.config import Scenario, StressAssumptions, load_config

V = {2025: dict(REVENUE=1000, OPERATING_INCOME=100, INTEREST_EXPENSE=20, BORROWINGS=1000)}
FULL_YEAR = StressAssumptions(floating_rate_share=1.0, months_applied=12, borrowings_basis="test",
                              fixed_cost_share=0.0)


def with_fixed(share):
    return FULL_YEAR.model_copy(update={"fixed_cost_share": share})


@pytest.fixture(scope="module")
def cfg():
    return load_config().stress


def sc(rev=0.0, margin=0.0, bp=0):
    return Scenario(description="t", revenue_change_pct=rev, op_margin_change_pctp=margin, rate_change_bp=bp)


def _run(cfg, values=V, assumptions=FULL_YEAR, **scs):
    return {r.scenario_id: r for r in run_stress(values, cfg, scenarios=scs, assumptions=assumptions)}


def test_base_equals_actual(cfg):
    base = _run(cfg)["BASE"]
    assert (base.revenue, base.operating_income, base.interest_expense) == (1000, 100, 20)
    assert base.interest_coverage == pytest.approx(5.0)
    assert base.delta_vs_base == dict.fromkeys(base.delta_vs_base, 0.0)


def test_combined_order_hand_calc(cfg):
    r = _run(cfg, S=sc(-20, -3, 200))["S"]
    # 매출 800 → 이익률 10%-3%p=7% → 영업이익 56, 이자 20 + 1000×1.0×2%×12/12 = 40
    assert r.revenue == pytest.approx(800)
    assert r.operating_income == pytest.approx(56)
    assert r.operating_margin_pct == pytest.approx(7.0)
    assert r.interest_expense == pytest.approx(40)
    assert r.interest_coverage == pytest.approx(1.4)
    assert r.delta_vs_base["operating_income"] == pytest.approx(-44)
    assert r.pretax_income_change == pytest.approx(-44 - 20)
    assert r.steps[0].startswith("매출 변화")


def test_revenue_change_keeps_margin(cfg):
    r = _run(cfg, S=sc(rev=-20))["S"]
    assert r.operating_margin_pct == pytest.approx(10.0) and r.operating_income == pytest.approx(80)


def test_revenue_minus_100_boundary(cfg):
    r = _run(cfg, S=sc(rev=-100))["S"]
    assert r.revenue == 0 and r.operating_income == 0
    assert r.operating_margin_pct is None  # 매출 0이면 이익률 정의 불가


def test_margin_turns_negative(cfg):
    r = _run(cfg, S=sc(margin=-15))["S"]
    assert r.operating_income == pytest.approx(-50) and r.interest_coverage == pytest.approx(-2.5)


@pytest.mark.parametrize("share,months,expected", [(0.0, 12, 20), (1.0, 0, 20), (0.5, 6, 25), (1.0, 12, 40)])
def test_rate_assumption_boundaries(cfg, share, months, expected):
    a = FULL_YEAR.model_copy(update={"floating_rate_share": share, "months_applied": months})
    assert _run(cfg, assumptions=a, S=sc(bp=200))["S"].interest_expense == pytest.approx(expected)


def test_rate_cut_clamped_at_zero(cfg):
    r = _run(cfg, S=sc(bp=-500))["S"]  # 증분 -50 > 기존 20
    assert r.interest_expense == 0 and r.interest_coverage_status == "NO_INTEREST_EXPENSE"
    assert any("0으로 제한" in n for n in r.notes)


def test_zero_interest_then_rate_up(cfg):
    v = {2025: {**V[2025], "INTEREST_EXPENSE": 0}}
    got = _run(cfg, values=v, S=sc(bp=100))
    assert got["BASE"].interest_coverage_status == "NO_INTEREST_EXPENSE"
    assert got["S"].interest_expense == pytest.approx(10) and got["S"].interest_coverage == pytest.approx(10.0)
    assert got["S"].delta_vs_base["interest_coverage"] is None  # BASE 배율이 없으므로 차이도 없음


def test_missing_borrowings_only_matters_for_rate(cfg):
    v = {2025: {k: x for k, x in V[2025].items() if k != "BORROWINGS"}}
    got = _run(cfg, values=v, R=sc(bp=100), S=sc(rev=-10))
    assert got["R"].status == "MISSING_INPUT" and "BORROWINGS" in got["R"].notes[0]
    assert got["S"].status == "OK"  # 금리 변화가 없으면 차입금 불필요


def test_missing_core_input(cfg):
    v = {2025: {k: x for k, x in V[2025].items() if k != "REVENUE"}}
    assert all(r.status == "MISSING_INPUT" and not r.delta_vs_base for r in run_stress(v, cfg))


def test_loss_company_revenue_drop_is_flagged(cfg):
    v = {2025: {**V[2025], "OPERATING_INCOME": -100}}
    got = _run(cfg, values=v, S=sc(rev=-20), U=sc(rev=10))
    assert got["S"].operating_income == pytest.approx(-80)  # 이익률 고정 가정의 결과
    assert any("고정비" in n for n in got["S"].notes)
    assert got["U"].notes == [] and got["BASE"].notes == []


# 매출 1000, 영업이익 100 → 영업비용 900. 고정비 50%: 고정 450, 변동비율 0.45
@pytest.mark.parametrize("share,rev,margin,expected", [
    (0.5, -20, 0, -10),     # 800 − 450 − 0.45×800
    (0.5, 0, 0, 100),       # 매출 변화 없으면 고정비 비중과 무관
    (0.5, -20, -3, -34),    # −10 − 800×3%
    (0.5, -100, 0, -450),   # 매출 0이면 고정비만 남음
    (1.0, -20, 0, -100),    # 800 − 900
    (0.5, 10, 0, 155),      # 1100 − 450 − 495 (영업레버리지로 이익 증가폭 확대)
])
def test_fixed_cost_hand_calc(cfg, share, rev, margin, expected):
    r = _run(cfg, assumptions=with_fixed(share), S=sc(rev=rev, margin=margin))["S"]
    assert r.operating_income == pytest.approx(expected)


def test_fixed_cost_makes_loss_company_worse(cfg):
    v = {2025: {**V[2025], "OPERATING_INCOME": -100}}  # 영업비용 1100
    r = _run(cfg, values=v, assumptions=with_fixed(0.5), S=sc(rev=-20))["S"]
    assert r.operating_income == pytest.approx(-190)  # 800 − 550 − 0.55×800: 손실 확대
    assert not any("고정비 비중 0" in n for n in r.notes)


def test_fixed_cost_does_not_change_base(cfg):
    a, b = _run(cfg)["BASE"], _run(cfg, assumptions=with_fixed(0.8))["BASE"]
    assert a.operating_income == b.operating_income == 100


def test_zero_revenue_base(cfg):
    v = {2025: {**V[2025], "REVENUE": 0, "OPERATING_INCOME": -30}}
    r = _run(cfg, values=v, S=sc(rev=-20, margin=-3))["S"]
    assert r.operating_income == -30 and r.notes


def test_default_scenarios_and_assumptions_recorded(cfg):
    results = run_stress(V, cfg)
    assert [r.scenario_id for r in results] == list(cfg.scenarios)
    combined = next(r for r in results if r.scenario_id == "COMBINED_SEVERE")
    # 설정 기본값: 변동비중 0.5, 12개월 → 이자 20 + 1000×0.5×2% = 30
    assert combined.interest_expense == pytest.approx(30)
    assert combined.assumptions.floating_rate_share == 0.5


def test_base_always_included(cfg):
    assert [r.scenario_id for r in run_stress(V, cfg, scenarios={"S": sc(rev=-5)})] == ["BASE", "S"]


def test_deterministic(cfg):
    assert [r.model_dump() for r in run_stress(V, cfg)] == [r.model_dump() for r in run_stress(V, cfg)]


def test_scenario_validation():
    with pytest.raises(ValueError):
        sc(rev=-100.1)
