"""AI API 실패·잘못된 응답 처리. 실제 API는 호출하지 않는다 (MockAdapter·가짜 SDK 클라이언트)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cif.report.generator import SYSTEM_PROMPT, generate_report, rule_based_draft
from cif.report.llm import AnthropicAdapter, LLMError, MockAdapter, api_schema
from cif.report.schema import AIDraft


def _good_json(inp) -> str:
    labels = {c: n[0] for c, n in inp["cfg"].accounts.accounts.items()}
    return rule_based_draft(inp["validation"].values(), inp["ratios"], inp["risks"], inp["stress"],
                            labels).model_dump_json()


def _with(raw_json: str, **changes) -> str:
    d = json.loads(raw_json)
    d.update(changes)
    return json.dumps(d, ensure_ascii=False)


@pytest.fixture
def inp(report_inputs):
    return report_inputs("DEMO02")


def test_valid_llm_response_used(inp):
    adapter = MockAdapter(_good_json(inp), model_name="mock-model")
    r = generate_report(**inp, adapter=adapter)
    assert r.provenance.ai_mode == "LLM" and r.provenance.ai_model == "mock-model"
    assert r.provenance.ai_error is None
    assert set(r.provenance.claim_counts) == {"VERIFIED"}
    system, user = adapter.calls[0]
    assert system == SYSTEM_PROMPT and "risk:OPERATING_LOSS_PERSISTENT" in user


@pytest.mark.parametrize("response,needle", [
    (LLMError("AI API 응답 시간 초과"), "시간 초과"),
    ("이것은 JSON이 아닙니다", "스키마"),
    ("", "스키마"),
    ('{"trend_commentary": []}', "스키마"),                       # 필수 필드 누락
    ('[1, 2, 3]', "스키마"),
])
def test_failures_fall_back_to_rule_based(inp, response, needle):
    r = generate_report(**inp, adapter=MockAdapter(response))
    assert r.provenance.ai_mode == "RULE_BASED_FALLBACK"
    assert needle in r.provenance.ai_error
    assert r.overall_opinion and all(c.status == "VERIFIED" for c in r.overall_opinion)


def test_extra_fields_rejected(inp):
    raw = _with(_good_json(inp), approval_decision="APPROVE")  # 스키마에 없는 결정 필드
    assert generate_report(**inp, adapter=MockAdapter(raw)).provenance.ai_mode == "RULE_BASED_FALLBACK"


def test_too_many_items_rejected(inp):
    claim = {"text": "x", "evidence_ids": ["risk:OPERATING_LOSS_PERSISTENT"]}
    raw = _with(_good_json(inp), positives=[claim] * 9)
    assert generate_report(**inp, adapter=MockAdapter(raw)).provenance.ai_mode == "RULE_BASED_FALLBACK"


def test_fabricated_number_marked(inp):
    bad = [{"text": "2025년 매출액은 99,999백만원입니다.", "evidence_ids": ["account:REVENUE:2025"]}]
    r = generate_report(**inp, adapter=MockAdapter(_with(_good_json(inp), trend_commentary=bad)))
    assert r.provenance.ai_mode == "LLM"
    assert r.trend_section.commentary[0].status == "UNVERIFIED_NUMBER"


def test_unknown_or_missing_evidence_marked(inp):
    claims = [{"text": "업계 1위 기업입니다.", "evidence_ids": []},
              {"text": "신용등급 AA입니다.", "evidence_ids": ["external:rating"]}]
    r = generate_report(**inp, adapter=MockAdapter(_with(_good_json(inp), positives=claims)))
    assert [c.status for c in r.positives] == ["UNSUPPORTED", "UNSUPPORTED"]


def test_decision_language_marked(inp):
    op = [{"text": "본 건 대출을 승인합니다.", "evidence_ids": ["risk:OPERATING_LOSS_PERSISTENT"]}]
    r = generate_report(**inp, adapter=MockAdapter(_with(_good_json(inp), overall_opinion=op)))
    assert r.overall_opinion[0].status == "POLICY_VIOLATION"
    assert r.provenance.claim_counts["POLICY_VIOLATION"] == 1


def test_ai_doc_requests_verified(inp):
    docs = [{"item": "차입금 명세서", "reason": "금리 민감도 확인", "evidence_ids": ["account:BORROWINGS:2025"]},
            {"item": "담보 감정서", "reason": "근거 없음", "evidence_ids": []}]
    r = generate_report(**inp, adapter=MockAdapter(_with(_good_json(inp), additional_documents=docs)))
    ai = [d for d in r.additional_documents if d.source == "AI"]
    assert [d.status for d in ai] == ["VERIFIED", "UNSUPPORTED"]


def test_api_key_never_in_prompt(inp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-should-not-leak")
    adapter = MockAdapter(_good_json(inp))
    generate_report(**inp, adapter=adapter)
    assert "sk-test-should-not-leak" not in "".join(s + u for s, u in adapter.calls)


# ---- AnthropicAdapter: 가짜 SDK 클라이언트로 응답 처리만 확인 ----

def _fake_client(response=None, exc=None):
    calls = []

    def create(**kw):
        calls.append(kw)
        if exc:
            raise exc
        return response
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))), calls


def _resp(text="{}", stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def adapter(monkeypatch):
    pytest.importorskip("anthropic", reason='선택 설치 항목: pip install -e ".[llm]"')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    return AnthropicAdapter()


def test_anthropic_request_shape(adapter):
    adapter._client, calls = _fake_client(_resp('{"ok": 1}'))
    assert adapter.complete("sys", "user", AIDraft.model_json_schema()) == '{"ok": 1}'
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    schema = kw["output_config"]["format"]["schema"]
    assert "title" not in json.dumps(schema) and "maxItems" not in json.dumps(schema)
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("resp,needle", [
    (_resp(stop="refusal"), "거절"),
    (_resp(stop="max_tokens"), "잘렸"),
    (_resp(text="  "), "비어"),
])
def test_anthropic_bad_responses(adapter, resp, needle):
    adapter._client, _ = _fake_client(resp)
    with pytest.raises(LLMError, match=needle):
        adapter.complete("s", "u", {})


def test_anthropic_errors_mapped(adapter):
    import anthropic
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc, needle in [(anthropic.APITimeoutError(request=req), "시간 초과"),
                        (anthropic.APIConnectionError(request=req), "연결")]:
        adapter._client, _ = _fake_client(exc=exc)
        with pytest.raises(LLMError, match=needle) as info:
            adapter.complete("s", "u", {})
        assert "test-not-a-real-key" not in str(info.value)


def test_missing_credentials_mapped(adapter):
    # 실제 SDK가 인증 정보 없이 호출될 때 던지는 예외 형태
    adapter._client, _ = _fake_client(exc=TypeError('"Could not resolve authentication method. Expected one of '
                                                    'api_key, auth_token, or credentials to be set."'))
    with pytest.raises(LLMError, match="인증 정보"):
        adapter.complete("s", "u", {})
    adapter._client, _ = _fake_client(exc=TypeError("unexpected keyword argument 'foo'"))
    with pytest.raises(TypeError):  # 코드 버그는 숨기지 않는다
        adapter.complete("s", "u", {})


def test_missing_credentials_falls_back(report_inputs, adapter):
    adapter._client, _ = _fake_client(exc=TypeError("Could not resolve authentication method."))
    r = generate_report(**report_inputs("DEMO02"), adapter=adapter)
    assert r.provenance.ai_mode == "RULE_BASED_FALLBACK" and "인증 정보" in r.provenance.ai_error


def test_api_schema_strips_constraints():
    assert api_schema({"title": "X", "type": "object", "properties": {"a": {"type": "string", "maxLength": 3}}}) == \
        {"type": "object", "properties": {"a": {"type": "string"}}}


@pytest.mark.llm_live
def test_live_claude_report(report_inputs):
    """실제 API 호출 (기본 실행 제외): pytest -m llm_live"""
    r = generate_report(**report_inputs("DEMO02"), adapter=AnthropicAdapter())
    out = Path(__file__).resolve().parents[1] / "outputs" / "live_report_DEMO02.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(r.model_dump_json(indent=2), encoding="utf-8")  # 결과 확인용 (outputs/는 git 제외)
    print(f"\nsaved {out}\nclaim_counts={r.provenance.claim_counts} ai_error={r.provenance.ai_error}")
    assert r.provenance.ai_mode == "LLM", r.provenance.ai_error
    assert "POLICY_VIOLATION" not in r.provenance.claim_counts
