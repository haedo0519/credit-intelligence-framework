"""보고서 생성. 수치·표는 코드가 만들고, AI는 근거 패키지에 대한 해석만 작성한다.

AI 호출이 실패하거나 응답이 스키마에 맞지 않으면 규칙 기반 문장으로 대체하고 그 사실을 기록한다.
어느 경우든 모든 서술은 verify_claim()을 거쳐 상태(VERIFIED 등)와 함께 저장된다.
"""
import json

from pydantic import ValidationError

from cif import __version__
from cif.analysis.ratios import SPECS, MetricResult
from cif.analysis.risk_rules import RuleEvaluation
from cif.analysis.stress import ScenarioResult
from cif.config import Config
from cif.ingest.validation import ValidationResult
from cif.report.evidence import Claim, ClaimResult, Pack, build_evidence, verify_claim
from cif.report.llm import LLMAdapter, LLMError
from cif.report.schema import (AIDraft, CreditReport, DocRequest, Overview, Provenance, Section, StressRow,
                               TableRow)

PROMPT_VERSION = "1.0.0"
TREND_ACCOUNTS = ["REVENUE", "OPERATING_INCOME", "NET_INCOME", "TOTAL_ASSETS", "TOTAL_LIABILITIES",
                  "TOTAL_EQUITY", "OPERATING_CASH_FLOW", "BORROWINGS"]

SYSTEM_PROMPT = """당신은 기업 여신심사 담당자를 돕는 재무분석 보조자입니다. 사용자 메시지의 근거 패키지(JSON)만 사용해 한국어로 해석을 작성합니다.

규칙:
- 모든 주장(claim)에는 근거 패키지에 있는 evidence_ids를 1개 이상 인용합니다. 패키지에 없는 사실은 쓰지 않습니다.
- 숫자는 인용한 근거의 값을 그대로 쓰거나, 인용한 두 근거의 차이·증감률만 씁니다. 금액은 원, 백만원 또는 억원으로 표기하고 표시 자릿수에 맞게 반올림합니다.
- status가 OK가 아닌 비율이나 value가 null인 근거는 수치로 쓰지 말고 "산출 불가"와 그 이유만 설명합니다.
- 위험 신호는 규칙 기반 점검 결과이며 부도확률이나 신용등급이 아닙니다. 확률·등급을 추정하지 않습니다.
- 대출 승인·거절·가결·부결을 결정하거나 권고하지 않습니다. 종합 의견은 확인된 강점, 위험, 추가 확인 사항을 정리하는 검토 의견입니다.
- additional_documents에는 판단에 필요하지만 패키지에 없는 자료(예: 차입금 명세, 감사보고서 주석)를 적습니다.
- 각 항목은 짧고 구체적으로 씁니다."""


def _labels(cfg: Config) -> dict[str, str]:
    return {code: names[0] for code, names in cfg.accounts.accounts.items()}


def _pack_json(pack: Pack) -> str:
    rows = [it.model_dump(exclude_defaults=True) for it in sorted(pack.values(), key=lambda i: i.id)]
    return json.dumps(rows, ensure_ascii=False, sort_keys=True)


def build_user_prompt(overview: Overview, pack: Pack) -> str:
    return (f"심사 건 개요: {overview.model_dump_json()}\n\n"
            f"근거 패키지 (id로 인용):\n{_pack_json(pack)}")


def _won(v: float) -> str:
    return f"{v / 1e6:,.0f}백만원"


def _c(text: str, ids: list[str]) -> dict:
    return {"text": text[:600], "evidence_ids": ids}  # AIClaim 길이 제한


def rule_based_draft(values, ratios: list[MetricResult], risks: list[RuleEvaluation],
                     stress: list[ScenarioResult], labels: dict[str, str]) -> AIDraft:
    """AI 없이 만드는 해석 문장. 근거 값을 그대로 옮기므로 검증을 통과해야 정상이다."""
    years = sorted(values)
    trend = []
    if len(years) >= 2:
        y0, y1 = years[0], years[-1]
        for code in ("REVENUE", "OPERATING_INCOME", "OPERATING_CASH_FLOW"):
            a, b = values[y0].get(code), values[y1].get(code)
            if a is not None and b is not None:
                trend.append(_c(f"{labels[code]}은 {y0}년 {_won(a)}에서 {y1}년 {_won(b)}으로 변동했습니다.",
                                [f"account:{code}:{y0}", f"account:{code}:{y1}"]))
    latest = [m for m in ratios if m.fiscal_year == years[-1]] if years else []
    ratio_c = [_c(f"{m.fiscal_year}년 {m.name}은 산출하지 않았습니다 ({m.status}: {m.note or '계산 조건 미충족'}).",
                  [m.evidence_id]) for m in latest if m.status != "OK"]
    positives = [_c(f"{e.description}: 해당 없음 ({e.message})", [f"risk:{e.rule_id}"])
                 for e in risks if e.status == "NOT_TRIGGERED" and e.rule_id != "DATA_INCONSISTENCY"]
    triggered = [e for e in risks if e.status == "TRIGGERED"]
    risk_c = [_c(f"[{e.severity}] {e.description}: {e.message}", [f"risk:{e.rule_id}"]) for e in triggered]
    stress_c = []
    for s in stress:
        if s.scenario_id == "BASE" or s.status != "OK":
            continue
        ids = [f"stress:{s.scenario_id}:{f}" for f in ("operating_income", "revenue_change_pct",
                                                        "op_margin_change_pctp", "rate_change_bp")]
        text = f"{s.description} 시 영업이익은 {_won(s.operating_income)}"
        if s.interest_coverage is not None:
            text += f", 이자보상배율은 {s.interest_coverage:.2f}배"
            ids.append(f"stress:{s.scenario_id}:interest_coverage")
        stress_c.append(_c(text + "로 계산됩니다.", ids))
    high = sum(e.severity == "HIGH" for e in triggered)
    unevaluated = [e for e in risks if e.status == "NOT_EVALUATED"]
    if triggered:
        opinions = [_c(f"규칙 기반 점검에서 위험 신호 {len(triggered)}건(HIGH {high}건)이 확인되어 "
                       f"해당 항목의 원인과 개선 계획 확인이 필요합니다.", [f"risk:{e.rule_id}" for e in triggered])]
    else:
        opinions = [_c("규칙 기반 점검에서 위험 신호가 확인되지 않았습니다.", [f"risk:{e.rule_id}" for e in risks])]
    if unevaluated:
        opinions.append(_c(f"자료 부족으로 판단하지 못한 규칙이 {len(unevaluated)}건 있습니다.",
                           [f"risk:{e.rule_id}" for e in unevaluated]))
    return AIDraft.model_validate({
        "trend_commentary": trend, "ratio_commentary": ratio_c[:8], "positives": positives[:8],
        "risk_commentary": risk_c[:10], "stress_commentary": stress_c[:8], "additional_documents": [],
        "overall_opinion": opinions, "limitations": [],
    })


DOC_FOR_ISSUE = {
    "BALANCE_MISMATCH": "감사보고서 원본 재무상태표",
    "PART_EXCEEDS_TOTAL": "감사보고서 원본 재무상태표",
    "SCOPE_YEAR_GAP": "누락 연도의 동일 범위(연결/별도) 재무제표",
    "SCOPE_MIXED": "연결·별도 재무제표 구분이 명확한 원본 자료",
    "FISCAL_MONTH_CHANGED": "결산월 변경 사유와 변경 전후 비교 가능 자료",
    "DUPLICATE_SAME": "중복 입력 여부를 확인할 수 있는 원본 자료",
}


def _rule_docs(validation: ValidationResult, risks: list[RuleEvaluation], labels) -> list[DocRequest]:
    docs: dict[str, DocRequest] = {}
    for i in validation.issues:
        if i.code == "MISSING_REQUIRED":
            item = f"{i.fiscal_year}년 {labels.get(i.account_code, i.account_code)} 자료"
            docs.setdefault(item, DocRequest(item=item, reason=i.message, source="RULE",
                                             evidence_ids=[f"issue:{i.code}:{i.fiscal_year}"]))
        elif i.code in DOC_FOR_ISSUE:
            item = DOC_FOR_ISSUE[i.code]
            docs.setdefault(item, DocRequest(item=item, reason=i.message, source="RULE",
                                             evidence_ids=[f"issue:{i.code}:{i.fiscal_year or 'all'}"]))
    for e in risks:
        if e.status == "NOT_EVALUATED":
            item = f"'{e.description}' 판단에 필요한 자료"
            docs.setdefault(item, DocRequest(item=item, reason=e.message, source="RULE",
                                             evidence_ids=[f"risk:{e.rule_id}"]))
    return list(docs.values())


def _limitations(validation: ValidationResult, cfg: Config) -> list[str]:
    a = cfg.stress.assumptions
    out = [
        f"재무제표 범위: {validation.scope} 기준으로만 분석했습니다.",
        "위험 기준은 업종을 구분하지 않은 일반 가정값이며 업종별 벤치마크와 비교하지 않았습니다.",
        "외부 신용정보, 담보, 비재무 정보(경영진, 산업 전망 등)는 반영하지 않았습니다.",
        f"스트레스 테스트는 법인세 효과를 반영하지 않으며 변동금리 비중 {a.floating_rate_share:.0%}, "
        f"적용 {a.months_applied}개월, 고정비 비중 {a.fixed_cost_share:.0%} 가정을 사용했습니다.",
    ]
    out += [f"[{i.level}] {i.message}" for i in validation.issues if i.level in ("WARNING", "INFO")]
    return out


def _table_accounts(values, labels) -> list[TableRow]:
    years = sorted(values)
    return [TableRow(code=c, label=labels.get(c, c), unit="원", cells={y: values[y].get(c) for y in years})
            for c in TREND_ACCOUNTS if any(c in values[y] for y in years)]


def _table_ratios(ratios: list[MetricResult]) -> list[TableRow]:
    rows = []
    for code, name, unit, *_ in SPECS:
        ms = [m for m in ratios if m.code == code]
        rows.append(TableRow(code=code, label=name, unit=unit, cells={m.fiscal_year: m.value for m in ms},
                             statuses={m.fiscal_year: m.status for m in ms if m.status != "OK"}))
    return rows


def generate_report(*, company_code: str, company_name: str, industry: str | None, case_no: str,
                    validation: ValidationResult, ratios: list[MetricResult], risks: list[RuleEvaluation],
                    stress: list[ScenarioResult], cfg: Config, input_hash: str,
                    adapter: LLMAdapter | None = None) -> CreditReport:
    if not validation.ok:
        raise ValueError("검증 오류가 있는 자료로는 보고서를 만들 수 없습니다")
    values = validation.values()
    labels = _labels(cfg)
    pack = build_evidence(values, ratios, risks, stress, validation.issues, cfg.risk, labels)
    overview = Overview(company_code=company_code, company_name=company_name, industry=industry, case_no=case_no,
                        scope=validation.scope, fiscal_years=sorted(values))

    draft, ai_error, mode = None, None, "RULE_BASED_FALLBACK"
    if adapter is not None:
        try:
            raw = adapter.complete(SYSTEM_PROMPT, build_user_prompt(overview, pack), AIDraft.model_json_schema())
            draft, mode = AIDraft.model_validate_json(raw), "LLM"
        except LLMError as e:
            ai_error = str(e)
        except ValidationError as e:
            ai_error = f"AI 응답이 보고서 스키마와 맞지 않습니다 ({e.error_count()}개 오류)"
    if draft is None:
        draft = rule_based_draft(values, ratios, risks, stress, labels)

    def check(claims) -> list[ClaimResult]:
        return [verify_claim(c, pack) for c in claims]

    ai_docs = []
    for d in draft.additional_documents:
        r = verify_claim(Claim(text=f"{d.item}: {d.reason}", evidence_ids=d.evidence_ids), pack)
        ai_docs.append(DocRequest(item=d.item, reason=d.reason, source="AI", evidence_ids=d.evidence_ids,
                                  status=r.status))
    sections = {
        "trend": check(draft.trend_commentary), "ratio": check(draft.ratio_commentary),
        "positives": check(draft.positives), "risk": check(draft.risk_commentary),
        "stress": check(draft.stress_commentary), "opinion": check(draft.overall_opinion),
        "limits": check(draft.limitations),
    }
    counts: dict[str, int] = {}
    for results in sections.values():
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
    for d in ai_docs:
        counts[d.status] = counts.get(d.status, 0) + 1

    a = cfg.stress.assumptions
    return CreditReport(
        overview=overview,
        limitations=_limitations(validation, cfg),
        ai_limitations=sections["limits"],
        trend=_table_accounts(values, labels), trend_section=Section(commentary=sections["trend"]),
        ratios=_table_ratios(ratios), ratio_section=Section(commentary=sections["ratio"]),
        positives=sections["positives"],
        risk_signals=[e for e in risks if e.status == "TRIGGERED"],
        risk_not_evaluated=[e for e in risks if e.status == "NOT_EVALUATED"],
        risk_section=Section(commentary=sections["risk"]),
        stress=[StressRow(scenario_id=s.scenario_id, description=s.description, status=s.status, revenue=s.revenue,
                          operating_income=s.operating_income, interest_expense=s.interest_expense,
                          interest_coverage=s.interest_coverage, pretax_income_change=s.pretax_income_change,
                          notes=s.notes) for s in stress],
        stress_assumptions=a.model_dump(),
        stress_section=Section(commentary=sections["stress"]),
        additional_documents=_rule_docs(validation, risks, labels) + ai_docs,
        overall_opinion=sections["opinion"],
        provenance=Provenance(
            app_version=__version__, input_hash=input_hash, config_hashes=cfg.hashes,
            rule_version=cfg.risk.version, stress_version=cfg.stress.version, ai_mode=mode,
            ai_model=adapter.model_name if adapter is not None else None, prompt_version=PROMPT_VERSION,
            ai_error=ai_error, claim_counts=dict(sorted(counts.items())), evidence_ids=sorted(pack),
        ),
    )
