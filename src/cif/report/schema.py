"""보고서 스키마.

AIDraft: AI가 작성하는 해석 부분 (모든 주장은 evidence_ids 필수, 승인·거절 필드 없음).
CreditReport: 10개 섹션 최종 보고서. 표·수치는 코드가 채우고, 서술은 검증을 거친 주장만 담는다.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cif.analysis.diagnosis import Diagnosis
from cif.analysis.risk_rules import RuleEvaluation
from cif.report.evidence import Claim, ClaimResult

DISCLAIMER = ("본 보고서는 포트폴리오용 분석 도구가 규칙과 공개된 계산식으로 산출한 참고 자료입니다. "
              "실제 금융기관의 심사 시스템이나 검증된 신용평가 모델이 아니며, 대출 승인·거절을 결정하지 않습니다. "
              "위험 신호는 부도확률이나 신용등급이 아닙니다.")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AIClaim(Claim, _Strict):
    text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str]


class AIDocRequest(_Strict):
    item: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=400)
    evidence_ids: list[str]


class AIDraft(_Strict):
    trend_commentary: list[AIClaim] = Field(max_length=8)
    ratio_commentary: list[AIClaim] = Field(max_length=8)
    positives: list[AIClaim] = Field(max_length=8)
    risk_commentary: list[AIClaim] = Field(max_length=10)
    stress_commentary: list[AIClaim] = Field(max_length=8)
    additional_documents: list[AIDocRequest] = Field(max_length=10)
    overall_opinion: list[AIClaim] = Field(min_length=1, max_length=6)
    limitations: list[AIClaim] = Field(max_length=6)


# ---- 최종 보고서 ----

class Overview(BaseModel):
    company_code: str
    company_name: str
    industry: str | None
    case_no: str
    scope: str
    fiscal_years: list[int]


class TableRow(BaseModel):
    code: str
    label: str
    unit: str
    cells: dict[int, float | None]      # 연도 → 값
    statuses: dict[int, str] = {}       # 연도 → 계산 상태 (OK가 아닌 경우)


class StressRow(BaseModel):
    scenario_id: str
    description: str
    status: str
    revenue: float | None
    operating_income: float | None
    interest_expense: float | None
    interest_coverage: float | None
    pretax_income_change: float | None
    notes: list[str]


class DocRequest(BaseModel):
    item: str
    reason: str
    source: Literal["RULE", "AI"]
    evidence_ids: list[str] = []
    status: str = "VERIFIED"


class Provenance(BaseModel):
    app_version: str
    input_hash: str
    config_hashes: dict[str, str]
    rule_version: str
    stress_version: str
    ai_mode: Literal["LLM", "RULE_BASED_FALLBACK"]
    ai_model: str | None
    prompt_version: str
    ai_error: str | None = None
    claim_counts: dict[str, int]
    evidence_ids: list[str]
    peer_inputs: dict[str, str] = {}   # 원인 진단에 쓴 비교 기업 → 입력 파일 SHA-256


class Section(BaseModel):
    commentary: list[ClaimResult] = []


class CreditReport(BaseModel):
    title: str = "AI 여신심사 검토 보고서 (참고용)"
    disclaimer: str = DISCLAIMER
    overview: Overview                                   # 1
    limitations: list[str]                               # 2 (코드가 작성)
    ai_limitations: list[ClaimResult]                    # 2 (AI 추가 한계)
    trend: list[TableRow]                                # 3
    trend_section: Section
    ratios: list[TableRow]                               # 4
    ratio_section: Section
    positives: list[ClaimResult]                         # 5
    risk_signals: list[RuleEvaluation]                   # 6 (TRIGGERED)
    risk_not_evaluated: list[RuleEvaluation]             # 6 (판단 불가)
    risk_section: Section
    stress: list[StressRow]                              # 7
    stress_assumptions: dict[str, float | int | str]
    stress_section: Section
    additional_documents: list[DocRequest]               # 8
    diagnoses: list[Diagnosis] = []                      # 9-1·9-2 원인 진단, 개선 계획 (이전 보고서와 호환)
    overall_opinion: list[ClaimResult]                   # 9-3
    provenance: Provenance                               # 10
