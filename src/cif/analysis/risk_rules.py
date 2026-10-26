"""규칙 기반 위험 신호 평가. 위험 신호는 부도확률·신용등급이 아니다.

모든 규칙은 TRIGGERED / NOT_TRIGGERED / NOT_EVALUATED 중 하나로 평가한다.
자료가 없어 판단할 수 없으면 '위험 없음'이 아니라 NOT_EVALUATED로 남긴다.
"""
from typing import Literal

from pydantic import BaseModel

from cif.analysis.ratios import MetricResult
from cif.config import RiskRules
from cif.ingest.mapping import Issue

RuleStatus = Literal["TRIGGERED", "NOT_TRIGGERED", "NOT_EVALUATED"]
# 재무자료 불일치·누락으로 보는 검증 이슈
DATA_ISSUE_CODES = {"BALANCE_MISMATCH", "PART_EXCEEDS_TOTAL", "MISSING_REQUIRED", "NEGATIVE_VALUE",
                    "SCOPE_YEAR_GAP", "FISCAL_MONTH_CHANGED"}


class RuleEvaluation(BaseModel):
    rule_id: str
    rule_version: str
    description: str
    fiscal_year: int
    status: RuleStatus
    severity: str | None  # TRIGGERED일 때만 값이 있음
    message: str
    evidence_ids: list[str]


def account_eid(code: str, year: int) -> str:
    return f"account:{code}:{year}"


class _Ctx:
    def __init__(self, values, ratios, issues, year, rules: RiskRules):
        self.values, self.issues, self.year, self.rules = values, issues, year, rules
        self.ratios = {(r.code, r.fiscal_year): r for r in ratios}

    def acct(self, code, year=None):
        return self.values.get(self.year if year is None else year, {}).get(code)

    def ratio(self, code, year=None) -> MetricResult | None:
        return self.ratios.get((code, self.year if year is None else year))

    def out(self, rule_id, status, message, evidence, severity=None) -> RuleEvaluation:
        rule = self.rules.rules[rule_id]
        if status == "TRIGGERED" and severity is None:
            severity = rule.severity
        return RuleEvaluation(rule_id=rule_id, rule_version=self.rules.version, description=rule.description,
                              fiscal_year=self.year, status=status,
                              severity=severity if status == "TRIGGERED" else None,
                              message=message, evidence_ids=evidence)


def _capital(ctx: _Ctx) -> list[RuleEvaluation]:
    te, cap = ctx.acct("TOTAL_EQUITY"), ctx.acct("PAID_IN_CAPITAL")
    ev = [account_eid("TOTAL_EQUITY", ctx.year), account_eid("PAID_IN_CAPITAL", ctx.year)]
    if te is None:
        msg = "자본총계 자료 없음"
        return [ctx.out("CAPITAL_IMPAIRMENT_FULL", "NOT_EVALUATED", msg, ev[:1]),
                ctx.out("CAPITAL_IMPAIRMENT_PARTIAL", "NOT_EVALUATED", msg, ev)]
    full = te <= 0
    res = [ctx.out("CAPITAL_IMPAIRMENT_FULL", "TRIGGERED" if full else "NOT_TRIGGERED",
                   f"자본총계 {te:,}원", ev[:1])]
    if full:  # 완전잠식이면 부분잠식은 중복 신호가 되므로 제외
        res.append(ctx.out("CAPITAL_IMPAIRMENT_PARTIAL", "NOT_TRIGGERED", "완전자본잠식으로 별도 판정", ev))
    elif cap is None:
        res.append(ctx.out("CAPITAL_IMPAIRMENT_PARTIAL", "NOT_EVALUATED", "자본금 자료 없음", ev))
    else:
        res.append(ctx.out("CAPITAL_IMPAIRMENT_PARTIAL", "TRIGGERED" if te < cap else "NOT_TRIGGERED",
                           f"자본총계 {te:,}원 / 자본금 {cap:,}원", ev))
    return res


def _operating_loss(ctx: _Ctx) -> RuleEvaluation:
    rid = "OPERATING_LOSS_PERSISTENT"
    n = int(ctx.rules.rules[rid].params["consecutive_years"])
    years = list(range(ctx.year - n + 1, ctx.year + 1))
    ev = [account_eid("OPERATING_INCOME", y) for y in years]
    ois = [ctx.acct("OPERATING_INCOME", y) for y in years]
    if ctx.acct("OPERATING_INCOME") is not None and ctx.acct("OPERATING_INCOME") >= 0:
        return ctx.out(rid, "NOT_TRIGGERED", f"{ctx.year}년 영업이익 {ctx.acct('OPERATING_INCOME'):,}원", ev[-1:])
    if any(v is None for v in ois):
        return ctx.out(rid, "NOT_EVALUATED", f"{n}개년 연속 영업이익 자료 없음 ({years[0]}~{ctx.year})", ev)
    hit = all(v < 0 for v in ois)
    return ctx.out(rid, "TRIGGERED" if hit else "NOT_TRIGGERED",
                   f"{years[0]}~{ctx.year} 영업이익: " + ", ".join(f"{v:,}" for v in ois), ev)


def _trend_rule(ctx: _Ctx, rid: str, ratio_code: str, min_key: str, drop_key: str, label: str, fmt: str,
                skip_status: tuple[str, ...] = ()) -> RuleEvaluation:
    """최근년도 값이 하한 미만이거나 전년 대비 하락폭이 기준 이상이면 신호."""
    p = ctx.rules.rules[rid].params
    cur, prev = ctx.ratio(ratio_code), ctx.ratio(ratio_code, ctx.year - 1)
    ev = [f"ratio:{ratio_code}:{ctx.year}"]
    if cur is not None and cur.status in skip_status:
        return ctx.out(rid, "NOT_TRIGGERED", f"{label}: {cur.status}", ev)
    if cur is None or cur.status != "OK":
        return ctx.out(rid, "NOT_EVALUATED", f"{label} 계산 불가 ({cur.status if cur else '자료 없음'})", ev)
    reasons = []
    if cur.value < p[min_key]:
        reasons.append(f"{label} {cur.value:{fmt}} < 기준 {p[min_key]:{fmt}}")
    if prev is not None and prev.status == "OK":
        ev.append(f"ratio:{ratio_code}:{ctx.year - 1}")
        drop = prev.value - cur.value
        if drop >= p[drop_key]:
            reasons.append(f"전년 대비 {drop:{fmt}} 하락 (기준 {p[drop_key]:{fmt}})")
    if reasons:
        return ctx.out(rid, "TRIGGERED", "; ".join(reasons), ev)
    return ctx.out(rid, "NOT_TRIGGERED", f"{label} {cur.value:{fmt}}", ev)


def _ocf(ctx: _Ctx) -> RuleEvaluation:
    rid = "NEGATIVE_OPERATING_CASH_FLOW"
    n = int(ctx.rules.rules[rid].params["escalate_after_years"])
    ocf = ctx.acct("OPERATING_CASH_FLOW")
    ev = [account_eid("OPERATING_CASH_FLOW", ctx.year)]
    if ocf is None:
        return ctx.out(rid, "NOT_EVALUATED", "영업활동현금흐름 자료 없음", ev)
    if ocf >= 0:
        return ctx.out(rid, "NOT_TRIGGERED", f"영업활동현금흐름 {ocf:,}원", ev)
    years = list(range(ctx.year - n + 1, ctx.year + 1))
    hist = [ctx.acct("OPERATING_CASH_FLOW", y) for y in years]
    if all(v is not None and v < 0 for v in hist):
        return ctx.out(rid, "TRIGGERED", f"{n}개년 연속 적자: " + ", ".join(f"{v:,}" for v in hist),
                       [account_eid("OPERATING_CASH_FLOW", y) for y in years], severity="HIGH")
    return ctx.out(rid, "TRIGGERED", f"영업활동현금흐름 {ocf:,}원", ev)


def _data(ctx: _Ctx) -> RuleEvaluation:
    rid = "DATA_INCONSISTENCY"
    found = [i for i in ctx.issues if i.code in DATA_ISSUE_CODES and i.level != "INFO"]
    if not found:
        return ctx.out(rid, "NOT_TRIGGERED", "불일치·누락 이슈 없음", [])
    ev = sorted({f"issue:{i.code}:{i.fiscal_year or 'all'}" for i in found})
    return ctx.out(rid, "TRIGGERED", f"검증 이슈 {len(found)}건: " + "; ".join(i.message for i in found[:5]), ev)


def evaluate_risks(values: dict[int, dict[str, int]], ratios: list[MetricResult], issues: list[Issue],
                   rules: RiskRules, fiscal_year: int | None = None) -> list[RuleEvaluation]:
    """기준 연도(기본: 최근년도)에 대해 모든 규칙을 평가한다."""
    if not values:
        return []
    year = max(values) if fiscal_year is None else fiscal_year
    ctx = _Ctx(values, ratios, issues, year, rules)
    return [
        *_capital(ctx),
        _operating_loss(ctx),
        _trend_rule(ctx, "INTEREST_COVERAGE_WEAK", "INTEREST_COVERAGE", "min_ratio", "max_drop",
                    "이자보상배율", ".2f", skip_status=("NO_INTEREST_EXPENSE",)),
        _trend_rule(ctx, "LIQUIDITY_DETERIORATION", "CURRENT_RATIO", "min_current_ratio_pct", "max_drop_pctp",
                    "유동비율", ".1f"),
        _ocf(ctx),
        _data(ctx),
    ]
