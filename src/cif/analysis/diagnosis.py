"""위험 신호 원인 진단.

1) 산업 공통인가 기업 고유인가: 같은 업종 비교 기업에 같은 규칙을 적용해 신호가 나온 비율로 판단
2) 수치로 본 원인: 정확히 성립하는 항등식으로만 분해 (예: 영업이익 변화 = 매출 효과 + 이익률 효과)
3) 개선 계획·확인 과제: 설정 파일의 규칙별 템플릿 (규칙 기반 제안)

AI는 계산하지 않는다. 여기서 만든 수치는 근거 패키지(diag:/peer: ID)로 전달된다.
"""
from typing import Literal

from pydantic import BaseModel

from cif.analysis.ratios import compute_ratios
from cif.analysis.risk_rules import RuleEvaluation, account_eid, evaluate_risks
from cif.config import RiskRules
from cif.fmt import won

Classification = Literal["INDUSTRY_COMMON", "COMPANY_SPECIFIC", "UNDETERMINED"]
CLASS_LABEL = {"INDUSTRY_COMMON": "산업 공통", "COMPANY_SPECIFIC": "기업 고유", "UNDETERMINED": "판단 불가"}


class Peer(BaseModel):
    code: str
    name: str
    values: dict[int, dict[str, int]]
    input_hash: str = ""


class PeerResult(BaseModel):
    code: str
    name: str
    status: Literal["TRIGGERED", "NOT_TRIGGERED", "NOT_EVALUATED", "NO_DATA"]
    message: str


class Driver(BaseModel):
    key: str
    label: str
    value: float
    unit: Literal["원", "%", "%p"]
    evidence_ids: list[str]


class Diagnosis(BaseModel):
    rule_id: str
    description: str
    severity: str
    fiscal_year: int
    classification: Classification
    peer_results: list[PeerResult]
    peer_summary: str
    drivers: list[Driver]
    driver_summary: str
    estimate_note: str = ""     # 자료로 확정할 수 없어 추가 확인이 필요한 부분
    remediation: list[str]
    checks: list[str]
    monitoring: str

    @property
    def classification_label(self) -> str:
        return CLASS_LABEL[self.classification]

    def driver_id(self, key: str) -> str:
        return f"diag:{self.rule_id}:{key}"


def peer_eid(code: str, rule_id: str) -> str:
    return f"peer:{code}:{rule_id}"


# ---- 비교 기업 ----

def _peer_results(rule_id: str, year: int, peers: list[Peer], rules: RiskRules) -> list[PeerResult]:
    out = []
    for p in peers:
        if year not in p.values:
            out.append(PeerResult(code=p.code, name=p.name, status="NO_DATA", message=f"{year}년 자료 없음"))
            continue
        evals = evaluate_risks(p.values, compute_ratios(p.values), [], rules, fiscal_year=year)
        e = next(e for e in evals if e.rule_id == rule_id)
        out.append(PeerResult(code=p.code, name=p.name, status=e.status, message=e.message))
    return out


def _classify(rule_id: str, results: list[PeerResult], rules: RiskRules) -> tuple[Classification, str]:
    if rule_id == "DATA_INCONSISTENCY":
        return "UNDETERMINED", "자료 품질 신호는 동종 기업과 비교하지 않습니다"
    evaluated = [r for r in results if r.status in ("TRIGGERED", "NOT_TRIGGERED")]
    if len(evaluated) < rules.diagnosis.min_peers:
        return "UNDETERMINED", "비교 가능한 동종 업종 기업이 없습니다"
    hit = [r for r in evaluated if r.status == "TRIGGERED"]
    share = len(hit) / len(evaluated)
    cls = "INDUSTRY_COMMON" if share >= rules.diagnosis.peer_common_share else "COMPANY_SPECIFIC"
    names = ", ".join(f"{r.name}({'해당' if r.status == 'TRIGGERED' else '해당 없음'})" for r in evaluated)
    return cls, (f"동종 비교 기업 {len(evaluated)}곳 중 {len(hit)}곳에서 같은 신호 → {CLASS_LABEL[cls]} 요인 "
                 f"[{names}]")


# ---- 원인 분해 (규칙별) ----

def _d(rule_id, key, label, value, unit, ids) -> Driver:
    return Driver(key=key, label=label, value=value, unit=unit, evidence_ids=ids)


def _ocf(rule_id: str, v: dict, y: int) -> tuple[list[Driver], str, str]:
    cur = v.get(y, {})
    ocf, ni = cur.get("OPERATING_CASH_FLOW"), cur.get("NET_INCOME")
    adj, wc, fin = cur.get("CFO_ADJUSTMENTS"), cur.get("CFO_WORKING_CAPITAL"), cur.get("FINANCING_CASH_FLOW")
    if ocf is None or ni is None:
        return [], "영업현금흐름 또는 당기순이익 자료가 없어 원인을 분해할 수 없습니다.", ""
    ids = lambda *codes: [account_eid(c, y) for c in codes]  # noqa: E731
    if adj is None or wc is None:
        gap = ni - ocf
        return ([_d(rule_id, "ni_minus_ocf", "당기순이익 − 영업현금흐름", gap, "원",
                    ids("NET_INCOME", "OPERATING_CASH_FLOW"))],
                f"당기순이익 {won(ni)} 대비 영업현금흐름 {won(ocf)}로 {won(gap)} 차이가 있습니다. "
                f"현금흐름표 세부 항목(조정·운전자본 변동) 자료가 없어 더 나누지 못했습니다.",
                "비현금 항목과 운전자본 변동 내역은 현금흐름표 원본으로 확인해야 합니다.")
    other = ocf - ni - adj - wc  # 이자·배당·법인세 수취·지급 등
    parts = [("net_income", "당기순이익", ni, ["NET_INCOME"]), ("adjustments", "조정(비현금 항목)", adj, ["CFO_ADJUSTMENTS"]),
             ("working_capital", "영업 자산·부채 변동", wc, ["CFO_WORKING_CAPITAL"]),
             ("other", "기타(이자·법인세 등)", other, ["OPERATING_CASH_FLOW", "NET_INCOME", "CFO_ADJUSTMENTS",
                                                 "CFO_WORKING_CAPITAL"])]
    drivers = [_d(rule_id, k, label, val, "원", ids(*codes)) for k, label, val, codes in parts]
    worst = min(drivers, key=lambda d: d.value)
    text = (f"영업현금흐름 {won(ocf)} = 당기순이익 {won(ni)} + 조정 {won(adj)} + 영업 자산·부채 변동 {won(wc)} "
            f"+ 기타 {won(other)}. 가장 큰 감소 요인은 {worst.label}({won(worst.value)})입니다.")
    if fin is not None:
        drivers.append(_d(rule_id, "financing", "재무활동 현금흐름", fin, "원", ids("FINANCING_CASH_FLOW")))
        if fin > 0:
            text += f" 같은 해 재무활동으로 {won(fin)}을 조달해 부족분을 메웠습니다."
    note = ("영업 자산·부채 변동의 세부 구성(매출채권·재고·금융업 채권 등)은 이 자료에 없어 주석으로 확인해야 합니다."
            if worst.key == "working_capital" else "")
    return drivers, text, note


def _oi_bridge(rule_id: str, v: dict, y0: int, y1: int) -> tuple[list[Driver], str]:
    """영업이익 변화 = 매출 효과 (매출 변화 × 기초 이익률) + 이익률 효과 (기말 매출 × 이익률 변화). 정확히 성립."""
    a, b = v.get(y0, {}), v.get(y1, {})
    r0, r1, o0, o1 = a.get("REVENUE"), b.get("REVENUE"), a.get("OPERATING_INCOME"), b.get("OPERATING_INCOME")
    if None in (r0, r1, o0, o1) or r0 <= 0 or r1 <= 0:
        return [], "매출 또는 영업이익 자료가 없어 원인을 분해할 수 없습니다."
    m0, m1 = o0 / r0, o1 / r1
    rev_effect, margin_effect = (r1 - r0) * m0, r1 * (m1 - m0)
    ids = [account_eid(c, yy) for c in ("REVENUE", "OPERATING_INCOME") for yy in (y0, y1)]
    drivers = [_d(rule_id, "revenue_change_pct", f"매출 증감률({y0}→{y1})", (r1 - r0) / r0 * 100, "%", ids),
               _d(rule_id, "margin_change_pctp", f"영업이익률 변화({y0}→{y1})", (m1 - m0) * 100, "%p", ids),
               _d(rule_id, "revenue_effect", "매출 효과", rev_effect, "원", ids),
               _d(rule_id, "margin_effect", "이익률 효과", margin_effect, "원", ids)]
    main = "매출 효과(판매 규모 변화)" if abs(rev_effect) >= abs(margin_effect) else "이익률 효과(원가·비용 구조 변화)"
    text = (f"{y0}→{y1} 영업이익 변화 {won(o1 - o0)} = 매출 효과 {won(rev_effect)} + 이익률 효과 {won(margin_effect)} "
            f"(매출 {(r1 - r0) / r0 * 100:+.1f}%, 영업이익률 {(m1 - m0) * 100:+.1f}%p). 변화의 더 큰 부분은 {main}입니다.")
    return drivers, text


def _change(rule_id, v, y0, y1, items) -> tuple[list[Driver], str]:
    """두 해 사이 계정 변동을 나란히 보여 준다. items: (key, label, code)."""
    drivers, parts = [], []
    for key, label, code in items:
        a, b = v.get(y0, {}).get(code), v.get(y1, {}).get(code)
        if a is None or b is None:
            continue
        drivers.append(_d(rule_id, key, f"{label} 증감({y0}→{y1})", b - a, "원",
                          [account_eid(code, y0), account_eid(code, y1)]))
        parts.append(f"{label} {won(b - a)}")
    return drivers, (f"{y0}→{y1} " + ", ".join(parts) + ".") if parts else "비교할 전기 자료가 없습니다."


def _drivers(e: RuleEvaluation, v: dict, rules: RiskRules) -> tuple[list[Driver], str, str]:
    y, rid = e.fiscal_year, e.rule_id
    if rid == "NEGATIVE_OPERATING_CASH_FLOW":
        return _ocf(rid, v, y)
    if rid == "OPERATING_LOSS_PERSISTENT":
        n = int(rules.rules[rid].params["consecutive_years"])
        d, t = _oi_bridge(rid, v, y - n, y) if (y - n) in v else _oi_bridge(rid, v, y - 1, y)
        return d, t, ""
    if rid == "INTEREST_COVERAGE_WEAK":
        d, t = _change(rid, v, y - 1, y, [("oi_change", "영업이익", "OPERATING_INCOME"),
                                          ("interest_change", "이자비용", "INTEREST_EXPENSE")])
        return d, t, ""
    if rid == "LIQUIDITY_DETERIORATION":
        d, t = _change(rid, v, y - 1, y, [("ca_change", "유동자산", "CURRENT_ASSETS"),
                                          ("cl_change", "유동부채", "CURRENT_LIABILITIES")])
        return d, t, ""
    if rid in ("CAPITAL_IMPAIRMENT_FULL", "CAPITAL_IMPAIRMENT_PARTIAL"):
        years = sorted(yy for yy in v if yy <= y)
        losses = [(yy, v[yy]["NET_INCOME"]) for yy in years if "NET_INCOME" in v[yy]]
        if not losses:
            return [], "당기순이익 자료가 없어 원인을 분해할 수 없습니다.", ""
        total = sum(x for _, x in losses)
        d = [_d(rid, "cumulative_net_income", f"누적 당기순이익({losses[0][0]}~{losses[-1][0]})", total, "원",
                [account_eid("NET_INCOME", yy) for yy, _ in losses])]
        return d, f"{losses[0][0]}~{losses[-1][0]} 누적 당기순이익 {won(total)}이 자본을 줄였습니다.", ""
    return [], "자료 품질 신호로, 검증 이슈 목록과 추가 확인 자료를 참고하세요.", ""


def diagnose(values: dict[int, dict[str, int]], risks: list[RuleEvaluation], rules: RiskRules,
             peers: list[Peer] | None = None) -> list[Diagnosis]:
    """신호가 발생한 규칙마다 원인 진단을 만든다."""
    out = []
    for e in risks:
        if e.status != "TRIGGERED":
            continue
        rule = rules.rules[e.rule_id]
        results = _peer_results(e.rule_id, e.fiscal_year, peers or [], rules)
        cls, peer_summary = _classify(e.rule_id, results, rules)
        drivers, text, note = _drivers(e, values, rules)
        out.append(Diagnosis(rule_id=e.rule_id, description=e.description, severity=e.severity or "",
                             fiscal_year=e.fiscal_year, classification=cls, peer_results=results,
                             peer_summary=peer_summary, drivers=drivers, driver_summary=text, estimate_note=note,
                             remediation=rule.remediation, checks=rule.checks, monitoring=rule.monitoring))
    return out
