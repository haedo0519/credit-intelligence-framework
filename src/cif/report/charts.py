"""보고서용 차트. CreditReport에 이미 들어 있는 표 값만 그린다 (새 계산 없음).

PNG를 data URI로 돌려주므로 화면용 HTML과 PDF에 같은 그림이 들어간다.
pyplot 대신 Figure 객체를 직접 써서 Streamlit의 여러 스레드에서도 안전하다.
"""
import base64
import io
import textwrap
from functools import cache
from pathlib import Path

from matplotlib import font_manager, rc_context
from matplotlib.figure import Figure

from cif.report.schema import CreditReport, TableRow

# 한글 글꼴 후보 (Windows 맑은 고딕, 리눅스·맥 나눔고딕)
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/Library/Fonts/NanumGothic.ttf"),
]
NAVY, SKY, RED, GRAY, AMBER = "#1f3a5f", "#5b8db8", "#c0392b", "#9aa5b1", "#d9822b"
def money_scale(report: CreditReport) -> tuple[str, float, int]:
    """표·차트 금액 단위: 금액이 10조원 이상이면 조원, 아니면 억원. (단위명, 나눌 값, 소수 자릿수)"""
    biggest = max((abs(v) for row in report.trend for v in row.cells.values() if v is not None), default=0)
    return ("조원", 1e12, 2) if biggest >= 1e13 else ("억원", 1e8, 1)
# 비율 계산 상태의 짧은 한글 표기 (차트·표 공통)
METRIC_STATUS = {"NOT_MEANINGFUL": "해석불가", "DIV_BY_ZERO": "분모 0", "MISSING_INPUT": "자료 없음",
                 "NO_INTEREST_EXPENSE": "이자 없음"}


def find_font() -> Path | None:
    return next((p for p in FONT_CANDIDATES if p.exists()), None)


@cache
def _font_family() -> str:
    font = find_font()
    if font is None:
        return "DejaVu Sans"  # 한글은 깨지지만 그림은 만들어진다
    font_manager.fontManager.addfont(str(font))
    return font_manager.FontProperties(fname=str(font)).get_name()


def _figure(w: float, h: float) -> Figure:
    fig = Figure(figsize=(w, h), dpi=150)
    fig.set_facecolor("white")
    return fig


def _style(ax, title: str):
    ax.set_title(title, fontsize=10, loc="left", color=NAVY, fontweight="bold")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color="#e4e7eb", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=8)


def _uri(fig: Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", metadata={"Software": None})
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _row(rows: list[TableRow], code: str) -> TableRow | None:
    return next((r for r in rows if r.code == code), None)


def _korean(fn):
    """한글 글꼴과 ASCII 마이너스(맑은 고딕에 유니코드 마이너스 글리프가 없음)로 그린다."""
    def wrapper(report):
        with rc_context({"font.family": _font_family(), "axes.unicode_minus": False}):
            return fn(report)
    wrapper.__name__ = fn.__name__
    return wrapper


@_korean
def revenue_margin(report: CreditReport) -> str | None:
    """매출액(막대, 금액 단위) + 영업이익률(선, %)."""
    years = report.overview.fiscal_years
    rev, margin = _row(report.trend, "REVENUE"), _row(report.ratios, "OPERATING_MARGIN")
    if rev is None or len(years) < 2:
        return None
    fig = _figure(6.4, 2.8)
    ax = fig.add_subplot()
    labels = [str(y) for y in years]
    unit, div, dec = money_scale(report)
    vals = [(rev.cells.get(y) or 0) / div for y in years]
    bars = ax.bar(labels, vals, color=SKY, width=0.55, label=f"매출액({unit})")
    # 막대 값은 막대 안쪽에 써서 위쪽의 이익률 표기와 겹치지 않게 한다
    ax.bar_label(bars, labels=[f"{v:,.{dec}f}" for v in vals], fontsize=7, label_type="center", color="white")
    _style(ax, "매출액과 영업이익률")
    ax.set_ylabel(unit, fontsize=8)
    if margin is not None:
        ax2 = ax.twinx()
        m = [margin.cells.get(y) for y in years]
        ax2.plot(labels, [v if v is not None else float("nan") for v in m], color=RED, marker="o", linewidth=2,
                 label="영업이익률(%)")
        for x, v in zip(labels, m):
            if v is not None:
                ax2.annotate(f"{v:.1f}%", (x, v), textcoords="offset points", xytext=(0, 6), ha="center",
                             fontsize=7, color=RED)
        ax2.axhline(0, color=GRAY, linewidth=0.8, linestyle="--")
        ax2.set_ylabel("%", fontsize=8)
        ax2.spines["top"].set_visible(False)
        ax2.tick_params(labelsize=8)
        fig.legend(loc="upper right", fontsize=7, frameon=False, ncols=2)
    return _uri(fig)


@_korean
def earnings_cash(report: CreditReport) -> str | None:
    """영업이익·당기순이익·영업활동현금흐름 (금액 단위, 묶음 막대)."""
    unit, div, dec = money_scale(report)
    years = report.overview.fiscal_years
    series = [(c, lbl, col) for c, lbl, col in (("OPERATING_INCOME", "영업이익", NAVY), ("NET_INCOME", "당기순이익", SKY),
                                                ("OPERATING_CASH_FLOW", "영업활동현금흐름", AMBER))
              if _row(report.trend, c) is not None]
    if not series or len(years) < 2:
        return None
    fig = _figure(6.4, 2.8)
    ax = fig.add_subplot()
    width = 0.8 / len(series)
    for i, (code, label, color) in enumerate(series):
        row = _row(report.trend, code)
        xs = [j + (i - (len(series) - 1) / 2) * width for j in range(len(years))]
        vals = [(row.cells.get(y) or 0) / div for y in years]
        bars = ax.bar(xs, vals, width=width * 0.95, color=color, label=label)
        ax.bar_label(bars, labels=[f"{v:,.1f}" for v in vals], fontsize=6, padding=2)
    ax.set_xticks(range(len(years)), [str(y) for y in years])
    ax.margins(y=0.15)
    ax.axhline(0, color="#52606d", linewidth=0.8)
    _style(ax, f"이익과 현금흐름 ({unit})")
    ax.legend(fontsize=7, frameon=False, ncols=3, loc="upper right")
    return _uri(fig)


KEY_RATIOS = [("CURRENT_RATIO", "유동비율 (%)"), ("DEBT_RATIO", "부채비율 (%)"),
              ("INTEREST_COVERAGE", "이자보상배율 (배)"), ("ROE", "ROE (%)")]


@_korean
def key_ratios(report: CreditReport) -> str | None:
    """핵심 비율 4개 소형 차트. 산출 불가 연도는 비워 두고 표시한다."""
    years = report.overview.fiscal_years
    if len(years) < 2:
        return None
    fig = _figure(6.4, 4.0)
    labels = [str(y) for y in years]
    for i, (code, title) in enumerate(KEY_RATIOS):
        ax = fig.add_subplot(2, 2, i + 1)
        row = _row(report.ratios, code)
        vals = [row.cells.get(y) if row else None for y in years]
        ax.plot(labels, [v if v is not None else float("nan") for v in vals], color=NAVY, marker="o", linewidth=2)
        for x, v, y in zip(labels, vals, years):
            status = row.statuses.get(y, "-") if row else "-"
            text = f"{v:,.1f}" if v is not None else METRIC_STATUS.get(status, status)
            ax.annotate(text, (x, v if v is not None else 0), textcoords="offset points", xytext=(0, 6),
                        ha="center", fontsize=6.5, color=NAVY if v is not None else RED)
        if any(v is not None and v < 0 for v in vals):
            ax.axhline(0, color=GRAY, linewidth=0.8, linestyle="--")
        _style(ax, title)
        ax.margins(y=0.25)
    fig.tight_layout(h_pad=1.5)
    return _uri(fig)


@_korean
def stress_impact(report: CreditReport) -> str | None:
    """시나리오별 영업이익(금액 단위, 가로 막대)과 이자보상배율 표기."""
    unit, div, dec = money_scale(report)
    rows = [s for s in report.stress if s.status == "OK" and s.operating_income is not None]
    if not rows:
        return None
    fig = _figure(6.4, 0.45 * len(rows) + 0.8)
    ax = fig.add_subplot()
    names = [textwrap.fill(s.description, 16) for s in rows][::-1]  # 긴 이름이 그림 폭을 줄이지 않게 줄바꿈
    vals = [s.operating_income / div for s in rows][::-1]
    colors = [GRAY if s.scenario_id == "BASE" else (RED if s.operating_income < 0 else SKY) for s in rows][::-1]
    bars = ax.barh(names, vals, color=colors, height=0.6)
    icr = [s.interest_coverage for s in rows][::-1]
    ax.bar_label(bars, labels=[f"{v:,.{dec}f}{unit} · ICR {c:.2f}배" if c is not None else f"{v:,.{dec}f}{unit}"
                               for v, c in zip(vals, icr)], fontsize=7, padding=3)
    ax.axvline(0, color="#52606d", linewidth=0.8)
    _style(ax, "스트레스 시나리오별 영업이익")
    ax.grid(axis="x", color="#e4e7eb", linewidth=0.8)
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.35)
    return _uri(fig)


def all_charts(report: CreditReport) -> dict[str, str | None]:
    return {"revenue_margin": revenue_margin(report), "earnings_cash": earnings_cash(report),
            "key_ratios": key_ratios(report), "stress_impact": stress_impact(report)}
