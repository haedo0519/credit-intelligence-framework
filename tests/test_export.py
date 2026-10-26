import base64
import json
import warnings

import pytest

from cif.config import load_config
from cif.db import repository as repo
from cif.export import draft_from_report, export_latest, import_ai_report
from cif.pipeline import load_run, run_analysis, upload_statements
from cif.report import charts
from cif.report.generator import generate_report
from cif.report.llm import MockAdapter
from cif.report.render import render_html


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c.settings = c.settings.model_copy(update={"raw_dir": str(tmp_path / "raw")})
    return c


@pytest.mark.parametrize("code", ["DEMO01", "DEMO03", "DEMO04"])
def test_charts_are_png_without_font_warnings(report_inputs, code):
    r = generate_report(**report_inputs(code))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 한글·마이너스 글리프 누락 경고가 나면 실패
        out = charts.all_charts(r)
    assert set(out) == {"revenue_margin", "earnings_cash", "key_ratios", "stress_impact"}
    for uri in out.values():
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"


def test_money_scale_switches_to_jo_won(report_inputs):
    r = generate_report(**report_inputs("DEMO01"))
    assert charts.money_scale(r) == ("억원", 1e8, 1)          # 시연 기업: 수백억원 규모
    big = r.model_copy(deep=True)
    big.trend[0].cells = {y: 175e12 for y in big.trend[0].cells}  # 175조원
    assert charts.money_scale(big) == ("조원", 1e12, 2)
    html = render_html(big)
    assert "175.00" in html and "단위: 조원" in html and "그림 1. 매출액(조원)" in html
    assert "{{" not in html and "}}" not in html  # 템플릿 변수가 글자로 남지 않아야 함


def test_charts_skip_when_single_year(report_inputs):
    inp = report_inputs("DEMO01")
    r = generate_report(**inp).model_copy(deep=True)
    r.overview.fiscal_years = [2025]
    assert charts.revenue_margin(r) is None and charts.key_ratios(r) is None


def test_html_embeds_charts_and_kpis(report_inputs):
    html = render_html(generate_report(**report_inputs("DEMO02")), issued="2026-10-26")
    assert html.count("data:image/png;base64,") == 4
    assert "핵심 요약" in html and "주요 지표" in html and "작성일 2026-10-26" in html


def _setup(conn, cfg, sample_dir, code="DEMO02"):
    cid = repo.create_company(conn, code, "가상유통(주)")
    case_id = repo.create_case(conn, cid, f"CASE-{code}")
    up = upload_statements(conn, cfg, cid, sample_dir / f"{code}.csv")
    return case_id, up.document_id


def test_draft_roundtrip_and_import(conn, cfg, sample_dir, tmp_path):
    case_id, doc_id = _setup(conn, cfg, sample_dir)
    run_analysis(conn, cfg, case_id, doc_id)
    # AI 응답 흉내: 규칙 기반 문장을 AI 응답으로 쓴 보고서를 JSON으로 저장
    base = load_run(conn, run_analysis(conn, cfg, case_id, doc_id)).report
    ai = generate_report(**_inputs_from_run(conn, cfg, case_id, doc_id),
                         adapter=MockAdapter(draft_from_report(base), model_name="claude-opus-5-5"))
    path = tmp_path / "ai.json"
    path.write_text(ai.model_dump_json(), encoding="utf-8")

    run_id = import_ai_report(conn, cfg, path)
    imported = load_run(conn, run_id).report
    assert imported.provenance.ai_mode == "LLM"
    assert imported.provenance.ai_model == "claude-opus-5-5 (저장된 응답 재사용)"
    assert [c.text for c in imported.overall_opinion] == [c.text for c in ai.overall_opinion]


def _inputs_from_run(conn, cfg, case_id, doc_id):
    from cif.analysis.ratios import compute_ratios
    from cif.analysis.risk_rules import evaluate_risks
    from cif.analysis.stress import run_stress
    from cif.ingest.validation import validate_file
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    v = validate_file(doc["stored_path"], cfg)
    ratios = compute_ratios(v.values())
    return dict(company_code="DEMO02", company_name="가상유통(주)", industry=None, case_no="CASE-DEMO02",
                validation=v, ratios=ratios, risks=evaluate_risks(v.values(), ratios, v.issues, cfg.risk),
                stress=run_stress(v.values(), cfg.stress), cfg=cfg, input_hash=doc["sha256"])


def test_import_rejects_rule_based_or_unknown(conn, cfg, sample_dir, tmp_path):
    case_id, doc_id = _setup(conn, cfg, sample_dir)
    rb = load_run(conn, run_analysis(conn, cfg, case_id, doc_id)).report
    p = tmp_path / "rb.json"
    p.write_text(rb.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="AI 응답"):
        import_ai_report(conn, cfg, p)
    data = json.loads(rb.model_dump_json())
    data["provenance"].update(ai_mode="LLM", input_hash="0" * 64)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="DB에 없습니다"):
        import_ai_report(conn, cfg, p)


def test_export_latest_per_case(conn, cfg, sample_dir, tmp_path):
    case_id, doc_id = _setup(conn, cfg, sample_dir)
    run_analysis(conn, cfg, case_id, doc_id)
    latest = run_analysis(conn, cfg, case_id, doc_id)
    files = export_latest(conn, tmp_path / "out", "2026-10-26")
    assert sorted(p.suffix for p in files) == [".html", ".pdf"]
    assert all(f"run{latest}" in p.name for p in files)  # 심사 건별 최신 실행만
    pdf = next(p for p in files if p.suffix == ".pdf")
    assert pdf.read_bytes()[:5] == b"%PDF-"
