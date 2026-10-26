"""재무비율 계산 엔진. AI를 사용하지 않으며 같은 입력이면 항상 같은 결과를 낸다.

불변식: status == "OK" 일 때만 value가 있고, 그 외에는 value=None (보고서에 수치로 쓰지 않음).
"""
from typing import Literal

from pydantic import BaseModel

Status = Literal["OK", "MISSING_INPUT", "DIV_BY_ZERO", "NOT_MEANINGFUL", "NO_INTEREST_EXPENSE"]


class MetricResult(BaseModel):
    code: str
    name: str
    fiscal_year: int
    value: float | None
    unit: Literal["%", "회", "배"]
    status: Status
    formula: str
    inputs: dict[str, int | None]
    basis: Literal["END", "AVERAGE"] = "END"  # 잔액 기준: 기말 또는 (전기말+당기말)/2
    note: str = ""

    @property
    def evidence_id(self) -> str:
        return f"ratio:{self.code}:{self.fiscal_year}"


# code, 이름, 단위, 계산식, 분자 계정, 분모 계정, 배수, 평균잔액 사용 여부
SPECS = [
    ("CURRENT_RATIO", "유동비율", "%", "유동자산 / 유동부채 × 100", ["CURRENT_ASSETS"], "CURRENT_LIABILITIES", 100, False),
    ("QUICK_RATIO", "당좌비율", "%", "(유동자산 − 재고자산) / 유동부채 × 100", ["CURRENT_ASSETS", "-INVENTORIES"], "CURRENT_LIABILITIES", 100, False),
    ("DEBT_RATIO", "부채비율", "%", "부채총계 / 자본총계 × 100", ["TOTAL_LIABILITIES"], "TOTAL_EQUITY", 100, False),
    ("EQUITY_RATIO", "자기자본비율", "%", "자본총계 / 자산총계 × 100", ["TOTAL_EQUITY"], "TOTAL_ASSETS", 100, False),
    ("OPERATING_MARGIN", "영업이익률", "%", "영업이익 / 매출액 × 100", ["OPERATING_INCOME"], "REVENUE", 100, False),
    ("ROA", "총자산이익률(ROA)", "%", "당기순이익 / 평균자산총계 × 100", ["NET_INCOME"], "TOTAL_ASSETS", 100, True),
    ("ROE", "자기자본이익률(ROE)", "%", "당기순이익 / 평균자본총계 × 100", ["NET_INCOME"], "TOTAL_EQUITY", 100, True),
    ("ASSET_TURNOVER", "총자산회전율", "회", "매출액 / 평균자산총계", ["REVENUE"], "TOTAL_ASSETS", 1, True),
    ("INTEREST_COVERAGE", "이자보상배율", "배", "영업이익 / 이자비용", ["OPERATING_INCOME"], "INTEREST_EXPENSE", 1, False),
    ("OCF_RATIO", "영업현금흐름 비율", "%", "영업활동현금흐름 / 유동부채 × 100", ["OPERATING_CASH_FLOW"], "CURRENT_LIABILITIES", 100, False),
]
# 자본이 0 이하이면 비율 자체가 해석 불가 (자본잠식은 위험 규칙에서 별도 탐지)
EQUITY_DENOMINATOR = {"DEBT_RATIO", "ROE"}


def _metric(spec, year: int, cur: dict[str, int], prev: dict[str, int] | None) -> MetricResult:
    code, name, unit, formula, num_terms, den_code, mult, use_avg = spec
    inputs: dict[str, int | None] = {}
    for t in num_terms:
        inputs[t.lstrip("-")] = cur.get(t.lstrip("-"))
    inputs[den_code] = cur.get(den_code)

    basis, note = "END", ""
    den_values = [cur.get(den_code)]
    if use_avg:
        if prev is not None and prev.get(den_code) is not None:
            basis = "AVERAGE"
            inputs[f"{den_code}_PRIOR"] = prev[den_code]
            den_values.append(prev[den_code])
        else:
            note = "전기 자료가 없어 기말잔액 기준으로 계산"

    def result(status, value=None, extra=""):
        return MetricResult(code=code, name=name, fiscal_year=year, value=value, unit=unit, status=status,
                            formula=formula, inputs=inputs, basis=basis, note=" / ".join(x for x in (note, extra) if x))

    if any(v is None for v in inputs.values()):
        missing = [k for k, v in inputs.items() if v is None]
        return result("MISSING_INPUT", extra=f"누락 계정: {', '.join(missing)}")
    if code in EQUITY_DENOMINATOR and min(den_values) <= 0:
        return result("NOT_MEANINGFUL", extra="자본총계가 0 이하")
    den = sum(den_values) / len(den_values)
    if den == 0:
        return result("NO_INTEREST_EXPENSE" if code == "INTEREST_COVERAGE" else "DIV_BY_ZERO")
    if den < 0:
        return result("NOT_MEANINGFUL", extra="분모가 음수")
    num = sum(-cur[t[1:]] if t.startswith("-") else cur[t] for t in num_terms)
    return result("OK", value=num / den * mult)


def compute_ratios(values: dict[int, dict[str, int]]) -> list[MetricResult]:
    """values: 연도 → 표준계정코드 → 원 단위 금액. 연도 오름차순, SPECS 순서로 반환."""
    out = []
    for year in sorted(values):
        prev = values.get(year - 1)
        out += [_metric(spec, year, values[year], prev) for spec in SPECS]
    return out
