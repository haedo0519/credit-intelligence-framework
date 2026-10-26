"""스트레스 테스트. 기준 연도 실적에 시나리오를 적용하고 BASE와의 차이를 계산한다.

적용 순서(고정): ① 매출 변화 → ② 변화된 매출에 영업이익률 변화폭 적용 → ③ 이자비용 변화
① 영업비용(매출 − 영업이익) = 고정비(× fixed_cost_share) + 변동비(매출 비례)
   변화 후 영업이익 = 변화 매출 − 고정비 − 변동비율 × 변화 매출   (고정비 0이면 이익률 고정과 동일)
이자비용 증분 = 차입금 × 변동금리 비중 × 금리 변화(bp)/10,000 × 적용개월/12
법인세 효과는 반영하지 않는다 (세전 이익 영향만 계산).
"""
from typing import Literal

from pydantic import BaseModel

from cif.config import Scenario, StressAssumptions, StressConfig

STEPS = ["매출 변화(고정비·변동비 반영)", "영업이익률 변화(변화된 매출 기준)", "이자비용 변화"]


class ScenarioResult(BaseModel):
    scenario_id: str
    description: str
    fiscal_year: int
    status: Literal["OK", "MISSING_INPUT"]
    scenario: Scenario
    assumptions: StressAssumptions
    inputs: dict[str, int | None]
    revenue: float | None = None
    operating_income: float | None = None
    operating_margin_pct: float | None = None
    interest_expense: float | None = None
    interest_coverage: float | None = None
    interest_coverage_status: Literal["OK", "NO_INTEREST_EXPENSE", "NOT_COMPUTED"] = "NOT_COMPUTED"
    pretax_income_change: float | None = None  # BASE 대비 (영업이익 변화 − 이자비용 변화)
    delta_vs_base: dict[str, float | None] = {}
    steps: list[str] = STEPS
    notes: list[str] = []


def _apply(sid: str, sc: Scenario, a: StressAssumptions, year: int, v: dict[str, int]) -> ScenarioResult:
    inputs = {k: v.get(k) for k in ("REVENUE", "OPERATING_INCOME", "INTEREST_EXPENSE", "BORROWINGS")}
    res = ScenarioResult(scenario_id=sid, description=sc.description, fiscal_year=year, status="OK",
                         scenario=sc, assumptions=a, inputs=inputs)
    needed = ["REVENUE", "OPERATING_INCOME", "INTEREST_EXPENSE"] + (["BORROWINGS"] if sc.rate_change_bp else [])
    missing = [k for k in needed if inputs[k] is None]
    if missing:
        res.status = "MISSING_INPUT"
        res.notes.append(f"누락 계정: {', '.join(missing)}")
        return res

    rev, oi, interest = inputs["REVENUE"], inputs["OPERATING_INCOME"], inputs["INTEREST_EXPENSE"]
    rev_s = rev * (1 + sc.revenue_change_pct / 100)
    if rev > 0:
        # 영업비용을 고정비와 변동비(매출 비례)로 나눈 뒤 변화된 매출로 재계산
        cost = rev - oi
        fixed = cost * a.fixed_cost_share
        variable_rate = (cost - fixed) / rev
        oi_s = rev_s - fixed - variable_rate * rev_s + rev_s * sc.op_margin_change_pctp / 100
        if sc.revenue_change_pct < 0 and oi < 0 and a.fixed_cost_share == 0:
            res.notes.append("고정비 비중 0(비용 전부 변동비) 가정에서는 영업손실 기업의 매출 감소가 손실 축소로 "
                             "계산됩니다. 고정비가 있으면 실제 손실은 더 커질 수 있습니다")
    else:
        oi_s = oi  # 매출이 0 이하이면 이익률을 정의할 수 없어 영업이익을 그대로 둔다
        if sc.revenue_change_pct or sc.op_margin_change_pctp:
            res.notes.append("기준 매출이 0 이하라 매출·이익률 변화를 적용하지 않음")

    increment = 0.0
    if sc.rate_change_bp:
        increment = inputs["BORROWINGS"] * a.floating_rate_share * sc.rate_change_bp / 10_000 * a.months_applied / 12
    interest_s = interest + increment
    if interest_s < 0:
        res.notes.append(f"금리 하락분({increment:,.0f}원)이 기존 이자비용보다 커 이자비용을 0으로 제한")
        interest_s = 0.0

    res.revenue, res.operating_income, res.interest_expense = rev_s, oi_s, interest_s
    res.operating_margin_pct = oi_s / rev_s * 100 if rev_s > 0 else None
    if interest_s > 0:
        res.interest_coverage, res.interest_coverage_status = oi_s / interest_s, "OK"
    else:
        res.interest_coverage_status = "NO_INTEREST_EXPENSE"
    return res


DELTA_FIELDS = ("revenue", "operating_income", "operating_margin_pct", "interest_expense", "interest_coverage")


def run_stress(values: dict[int, dict[str, int]], cfg: StressConfig, fiscal_year: int | None = None,
               scenarios: dict[str, Scenario] | None = None,
               assumptions: StressAssumptions | None = None) -> list[ScenarioResult]:
    """scenarios/assumptions를 넘기면 설정 파일 대신 사용한다 (화면에서 조정한 값). BASE는 항상 포함."""
    if not values:
        return []
    year = max(values) if fiscal_year is None else fiscal_year
    a = assumptions or cfg.assumptions
    chosen = {"BASE": cfg.scenarios["BASE"], **(scenarios if scenarios is not None else cfg.scenarios)}
    results = [_apply(sid, sc, a, year, values.get(year, {})) for sid, sc in chosen.items()]
    base = results[0]
    for r in results:
        if r.status != "OK" or base.status != "OK":
            continue
        r.delta_vs_base = {
            f: (None if getattr(r, f) is None or getattr(base, f) is None else getattr(r, f) - getattr(base, f))
            for f in DELTA_FIELDS
        }
        r.pretax_income_change = (r.operating_income - base.operating_income) - (r.interest_expense - base.interest_expense)
    return results
