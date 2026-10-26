"""근거 패키지 생성과 AI 주장(claim) 검증.

AI에게는 이 패키지만 전달한다. AI의 각 주장은 evidence_id를 인용해야 하며,
본문에 쓴 숫자는 인용한 근거의 값(또는 두 근거 간 차이·증감률)과 표시 정밀도 안에서 일치해야 한다.
"""
import re
from itertools import combinations, permutations
from typing import Literal

from pydantic import BaseModel

from cif.analysis.diagnosis import Diagnosis, peer_eid
from cif.analysis.ratios import MetricResult
from cif.analysis.risk_rules import RuleEvaluation
from cif.analysis.stress import ScenarioResult
from cif.config import RiskRules
from cif.ingest.mapping import Issue


class EvidenceItem(BaseModel):
    id: str
    kind: Literal["account", "ratio", "risk", "rule_param", "stress", "issue", "diagnosis", "peer"]
    label: str
    fiscal_year: int | None = None
    value: float | None = None
    unit: str = ""
    status: str = "OK"
    detail: str = ""
    related: list[str] = []  # 이 근거를 인용하면 함께 인용한 것으로 보는 하위 근거


Pack = dict[str, EvidenceItem]

STRESS_FIELDS = {"revenue": ("매출액", "원"), "operating_income": ("영업이익", "원"),
                 "operating_margin_pct": ("영업이익률", "%"), "interest_expense": ("이자비용", "원"),
                 "interest_coverage": ("이자보상배율", "배"), "pretax_income_change": ("세전이익 변화(BASE 대비)", "원")}
SCENARIO_PARAMS = {"revenue_change_pct": ("매출 변화율", "%"), "op_margin_change_pctp": ("영업이익률 변화폭", "%p"),
                   "rate_change_bp": ("금리 변화", "bp")}


def build_evidence(values: dict[int, dict[str, int]], ratios: list[MetricResult], risks: list[RuleEvaluation],
                   stress: list[ScenarioResult], issues: list[Issue], rules: RiskRules,
                   account_labels: dict[str, str], diagnoses: list[Diagnosis] | None = None) -> Pack:
    items: list[EvidenceItem] = []
    for year, accts in sorted(values.items()):
        for code, amount in sorted(accts.items()):
            items.append(EvidenceItem(id=f"account:{code}:{year}", kind="account", label=account_labels.get(code, code),
                                      fiscal_year=year, value=amount, unit="원"))
    for m in ratios:
        items.append(EvidenceItem(id=m.evidence_id, kind="ratio", label=m.name, fiscal_year=m.fiscal_year,
                                  value=m.value, unit=m.unit, status=m.status,
                                  detail=f"{m.formula} (잔액기준 {m.basis}) {m.note}".strip()))
    for rid, rule in rules.rules.items():
        for name, v in rule.params.items():
            items.append(EvidenceItem(id=f"rule:{rid}:{name}", kind="rule_param", label=f"{rule.description} 기준 {name}",
                                      value=v, status="OK", detail=f"규칙 버전 {rules.version}"))
    for e in risks:
        params = [f"rule:{e.rule_id}:{n}" for n in rules.rules[e.rule_id].params]
        items.append(EvidenceItem(id=f"risk:{e.rule_id}", kind="risk", label=e.description, fiscal_year=e.fiscal_year,
                                  status=e.status, detail=f"[{e.severity or '-'}] {e.message}",
                                  related=e.evidence_ids + params))
    for s in stress:
        for field, (label, unit) in STRESS_FIELDS.items():
            items.append(EvidenceItem(id=f"stress:{s.scenario_id}:{field}", kind="stress",
                                      label=f"{s.description} - {label}", fiscal_year=s.fiscal_year,
                                      value=getattr(s, field), unit=unit, status=s.status, detail=" ".join(s.notes)))
        for field, (label, unit) in SCENARIO_PARAMS.items():
            items.append(EvidenceItem(id=f"stress:{s.scenario_id}:{field}", kind="stress",
                                      label=f"{s.description} - {label}", value=getattr(s.scenario, field), unit=unit))
    grouped: dict[str, list[Issue]] = {}
    for i in issues:
        grouped.setdefault(f"issue:{i.code}:{i.fiscal_year or 'all'}", []).append(i)
    for eid, iss in grouped.items():
        items.append(EvidenceItem(id=eid, kind="issue", label=iss[0].code, fiscal_year=iss[0].fiscal_year,
                                  status=iss[0].level, detail=" / ".join(i.message for i in iss)))
    for dg in diagnoses or []:
        driver_ids = [dg.driver_id(d.key) for d in dg.drivers]
        peer_ids = [peer_eid(p.code, dg.rule_id) for p in dg.peer_results]
        for d in dg.drivers:
            items.append(EvidenceItem(id=dg.driver_id(d.key), kind="diagnosis", label=f"{dg.description} - {d.label}",
                                      fiscal_year=dg.fiscal_year, value=d.value, unit=d.unit, related=d.evidence_ids))
        for p in dg.peer_results:
            items.append(EvidenceItem(id=peer_eid(p.code, dg.rule_id), kind="peer",
                                      label=f"{p.name} - {dg.description}", fiscal_year=dg.fiscal_year,
                                      status=p.status, detail=p.message))
        items.append(EvidenceItem(id=dg.driver_id("summary"), kind="diagnosis", label=f"{dg.description} - 원인 진단",
                                  fiscal_year=dg.fiscal_year, status=dg.classification,
                                  detail=f"{dg.classification_label}: {dg.peer_summary} {dg.driver_summary} "
                                         f"{dg.estimate_note}".strip(),
                                  related=driver_ids + peer_ids + [f"risk:{dg.rule_id}"]))
    pack = {it.id: it for it in items}
    assert len(pack) == len(items), "evidence id 중복"
    return pack


# ---- 주장 검증 ----

ClaimStatus = Literal["VERIFIED", "UNSUPPORTED", "UNVERIFIED_NUMBER", "POLICY_VIOLATION"]

# 대출 승인·거절을 결정하거나 권고하는 표현 (의사록 '이사회 승인' 같은 일반 용어는 허용)
DECISION_PATTERNS = [
    re.compile(r"(대출|여신|신청|본\s*건|심사)\s*(을|를|은|는|이|가)?\s*(승인|거절|부결|가결|반려)"),
    re.compile(r"(승인|거절|부결|가결|반려)\s*(을|를)?\s*(권고|권장|제안|합니다|한다|함|하는\s*것이|해야|할\s*것)"),
    re.compile(r"대출\s*(이)?\s*(가능|불가)"),
    re.compile(r"\b(approve|approval|reject|decline)\b", re.I),
]

NUM_RE = re.compile(
    r"(?<![\w.])([-−△]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-−△]?\d+(?:\.\d+)?)\s*"
    r"(조원|억원|백만원|천원|원|%p|％p|%|％|배|회|bp|개년|년|개월|개|건|곳|월|분기|위)?"
)
IGNORED_UNITS = {"개년", "년", "개월", "개", "건", "곳", "월", "분기", "위"}  # 연도·개수는 검증 대상 아님
WON_SCALE = {"조원": 1e12, "억원": 1e8, "백만원": 1e6, "천원": 1e3, "원": 1.0}
UNIT_GROUP = {"%": "%", "％": "%", "%p": "%", "％p": "%", "배": "배", "회": "회", "bp": "bp"}


class Claim(BaseModel):
    text: str
    evidence_ids: list[str]


class ClaimResult(BaseModel):
    text: str
    evidence_ids: list[str]
    status: ClaimStatus
    reason: str = ""


YEAR_RANGE_RE = re.compile(r"(?:19|20)\d{2}\s*[~∼–-]\s*(?=(?:19|20)\d{2}\s*년)")  # '2024~2025년'의 앞 연도


def _numbers(text: str) -> list[tuple[float, int, str]]:
    """(값, 소수 자릿수, 단위) 목록."""
    out = []
    for m in NUM_RE.finditer(YEAR_RANGE_RE.sub(" ", text)):
        raw, unit = m.group(1), m.group(2) or ""
        if unit in IGNORED_UNITS:
            continue
        digits = raw.lstrip("-−△").replace(",", "")
        dec = len(digits.split(".")[1]) if "." in digits else 0
        out.append((abs(float(digits)), dec, unit))
    return out


def _candidates(items: list[EvidenceItem], group: str | None) -> list[float]:
    """비교 대상 값: 근거 값과 두 근거 간 차이·증감률(%)의 절댓값."""
    by_unit: dict[str, list[float]] = {}
    for it in items:
        if it.value is not None:
            by_unit.setdefault(UNIT_GROUP.get(it.unit, it.unit), []).append(it.value)
    out = []
    for unit, vals in by_unit.items():
        if group is None or unit == group:
            out += [abs(v) for v in vals] + [abs(a - b) for a, b in combinations(vals, 2)]
        if group in (None, "%"):  # 증감률(%)은 같은 단위의 두 값으로 계산 (예: 매출 증가율)
            out += [abs((b - a) / a * 100) for a, b in permutations(vals, 2) if a]
    return out


def _matches(num: float, dec: int, unit: str, items: list[EvidenceItem]) -> bool:
    if unit in WON_SCALE:
        scale, tol = WON_SCALE[unit], 0.5 * 10 ** -dec * WON_SCALE[unit]
        cands = _candidates([i for i in items if i.unit == "원"], "원")
        return any(abs(num * scale - c) <= tol + 1e-9 * c for c in cands)
    tol = 0.5 * 10 ** -dec
    if unit:
        return any(abs(num - c) <= tol + 1e-9 * c for c in _candidates(items, UNIT_GROUP[unit]))
    # 단위 없는 숫자: 모든 근거와 원 단위 금액의 백만원·억원 환산값까지 허용
    cands = _candidates(items, None)
    cands += [c / s for c in _candidates([i for i in items if i.unit == "원"], "원") for s in (1e6, 1e8, 1e12)]
    return any(abs(num - c) <= tol + 1e-9 * c for c in cands)


def _expand(ids: list[str], pack: Pack) -> list[EvidenceItem]:
    seen, stack, out = set(), list(ids), []
    while stack:
        eid = stack.pop()
        if eid in seen or eid not in pack:
            continue
        seen.add(eid)
        out.append(pack[eid])
        stack += pack[eid].related
    return out


def verify_claim(claim: Claim, pack: Pack) -> ClaimResult:
    def res(status, reason=""):
        return ClaimResult(text=claim.text, evidence_ids=claim.evidence_ids, status=status, reason=reason)

    for p in DECISION_PATTERNS:
        if p.search(claim.text):
            return res("POLICY_VIOLATION", "대출 승인·거절 결정 표현은 사용할 수 없습니다")
    if not claim.evidence_ids:
        return res("UNSUPPORTED", "인용한 근거가 없습니다")
    unknown = [e for e in claim.evidence_ids if e not in pack]
    if unknown:
        return res("UNSUPPORTED", f"존재하지 않는 근거: {', '.join(unknown)}")
    items = _expand(claim.evidence_ids, pack)
    # 인용한 근거의 설명문에 적힌 숫자(예: 규칙 설명 "유동비율 100% 미만")를 그대로 옮긴 것도 허용
    quoted = [n for it in items for n, _, _ in _numbers(f"{it.label} {it.detail}")]
    bad = [f"{n:g}{u}" for n, d, u in _numbers(claim.text)
           if not _matches(n, d, u, items) and not any(abs(n - q) <= 0.5 * 10 ** -d for q in quoted)]
    if bad:
        return res("UNVERIFIED_NUMBER", f"근거와 일치하지 않는 수치: {', '.join(bad)}")
    return res("VERIFIED")
