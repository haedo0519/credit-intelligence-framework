from pathlib import Path

import pytest
from pypdf import PdfReader

from cif.report.evidence import ClaimResult
from cif.report.generator import generate_report
from cif.report.render import RenderError, find_font, render_html, render_pdf


@pytest.fixture(scope="module")
def report(report_inputs):
    return generate_report(**report_inputs("DEMO02"))


def test_html_has_all_sections(report):
    html = render_html(report)
    for n, title in enumerate(["기업 및 심사 건 개요", "분석 범위와 자료의 한계", "최근 3개년 재무 추세", "주요 재무비율",
                               "긍정적인 요소", "주요 위험 신호", "스트레스 테스트 결과", "추가 확인이 필요한 자료",
                               "종합적인 심사 검토 의견", "근거 데이터 및 사용한 규칙 버전"], start=1):
        assert f"{n}. {title}" in html
    assert report.provenance.input_hash in html and "승인·거절을 결정하지 않습니다" in html
    assert "font-face" not in html  # 화면용 HTML은 로컬 글꼴 경로를 넣지 않는다


def test_html_escapes_text_and_separates_unverified(report):
    r = report.model_copy(deep=True)
    r.positives = [
        ClaimResult(text="<script>alert(1)</script> 확인된 문장", evidence_ids=["x"], status="VERIFIED"),
        ClaimResult(text="지어낸 수치 999억원", evidence_ids=["x"], status="UNVERIFIED_NUMBER", reason="불일치"),
        ClaimResult(text="본 건 대출을 승인합니다", evidence_ids=["x"], status="POLICY_VIOLATION", reason="결정 표현"),
    ]
    html = render_html(r)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "검증되지 않은 문장" in html and "[수치 불일치] 지어낸 수치 999억원" in html
    assert "본 건 대출을 승인합니다" not in html  # 정책 위반 문장은 본문을 싣지 않는다


def test_non_ok_ratio_shows_status(report_inputs):
    html = render_html(generate_report(**report_inputs("DEMO03")))
    assert "해석불가" in html  # NOT_MEANINGFUL을 짧은 한글로 표시


@pytest.mark.skipif(find_font() is None, reason="한글 글꼴 없음")
def test_pdf_korean_text(report, tmp_path):
    pdf = render_pdf(report)
    assert pdf[:5] == b"%PDF-"
    p = tmp_path / "r.pdf"
    p.write_bytes(pdf)
    text = "".join(page.extract_text() for page in PdfReader(p).pages)
    assert "종합적인 심사 검토 의견" in text and "가상유통" in text
    assert "■" not in text  # 글꼴이 빠지면 한글이 네모로 나온다


def test_pdf_without_font_raises(report):
    with pytest.raises(RenderError, match="글꼴"):
        render_pdf(report, font_path=Path("no-such-font.ttf"))
