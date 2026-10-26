"""보고서 HTML·PDF 출력. PDF는 한글 글꼴이 있어야 하며, 없으면 RenderError (HTML은 항상 가능)."""
import io
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cif.report.charts import METRIC_STATUS, all_charts, find_font
from cif.report.schema import CreditReport

TEMPLATES = Path(__file__).with_name("templates")
STATUS_LABEL = {"VERIFIED": "확인됨", "UNSUPPORTED": "근거 없음", "UNVERIFIED_NUMBER": "수치 불일치",
                "POLICY_VIOLATION": "정책 위반"}
SCOPE_LABEL = {"CFS": "연결재무제표", "OFS": "별도재무제표"}
# 요약 페이지 주요 지표: (표시명, 표 종류, 코드)
KPIS = [("매출액", "trend", "REVENUE"), ("영업이익", "trend", "OPERATING_INCOME"),
        ("영업활동현금흐름", "trend", "OPERATING_CASH_FLOW"), ("영업이익률 (%)", "ratios", "OPERATING_MARGIN"),
        ("부채비율 (%)", "ratios", "DEBT_RATIO"), ("유동비율 (%)", "ratios", "CURRENT_RATIO"),
        ("이자보상배율 (배)", "ratios", "INTEREST_COVERAGE"), ("ROE (%)", "ratios", "ROE")]


class RenderError(Exception):
    pass


def _eok(v) -> str:
    return "-" if v is None else f"{v / 1e8:,.1f}"


def _ratio(v) -> str:
    return "-" if v is None else f"{v:,.2f}"


_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
_env.filters.update(eok=_eok, ratio=_ratio, mstatus=lambda s: METRIC_STATUS.get(s, s))


def _bold(font: Path) -> Path:
    """같은 폴더의 굵은 글꼴 (malgun.ttf → malgunbd.ttf, NanumGothic.ttf → NanumGothicBold.ttf). 없으면 보통 글꼴."""
    for name in (f"{font.stem}bd{font.suffix}", f"{font.stem}Bold{font.suffix}"):
        if (font.parent / name).exists():
            return font.parent / name
    return font


def render_html(report: CreditReport, font_path: Path | None = None, issued: str | None = None) -> str:
    """font_path를 주면 PDF용 글꼴 선언을 넣는다 (화면용 HTML은 None). issued는 표지에 적을 작성일."""
    tables = {"trend": report.trend, "ratios": report.ratios}
    kpis = [(label, next((r for r in tables[t] if r.code == code), None), "won" if t == "trend" else "ratio")
            for label, t, code in KPIS]
    return _env.get_template("report.html").render(
        r=report, p=report.provenance, years=report.overview.fiscal_years, status_label=STATUS_LABEL,
        scope_label=SCOPE_LABEL, charts=all_charts(report), kpis=kpis, issued=issued,
        high_count=sum(e.severity == "HIGH" for e in report.risk_signals),
        medium_count=sum(e.severity == "MEDIUM" for e in report.risk_signals),
        font_path=font_path.as_posix() if font_path else None,
        bold_font_path=_bold(font_path).as_posix() if font_path else None)


def render_pdf(report: CreditReport, font_path: Path | None = None, issued: str | None = None) -> bytes:
    from xhtml2pdf import pisa
    from xhtml2pdf.config.resources import ResourceAccessPolicy

    font = font_path or find_font()
    if font is None or not Path(font).exists():
        raise RenderError("한글 글꼴을 찾지 못해 PDF를 만들 수 없습니다. HTML로 내려받아 주세요.")
    # 본문에 AI 문장이 들어가므로 네트워크 금지, 로컬 읽기는 글꼴 폴더만 허용 (차트는 data: URI)
    policy = ResourceAccessPolicy(allow_remote=False, base_dir=None, extra_roots=(Path(font).parent,))
    buf = io.BytesIO()
    status = pisa.CreatePDF(render_html(report, Path(font), issued), dest=buf, encoding="utf-8",
                            resource_policy=policy)
    if status.err:
        raise RenderError(f"PDF 변환 오류 {status.err}건")
    return buf.getvalue()
