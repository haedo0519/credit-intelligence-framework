"""데이터셋 단위 검증: 기업코드·범위(연결/별도) 선택·결산일·중복·결측·이상치·정합성.

ERROR가 하나라도 있으면 ok=False이며 분석을 진행하지 않는다 (부분 데이터로 조용히 분석하지 않음).
WARNING/INFO는 분석을 막지 않지만 결과와 보고서의 '자료의 한계'에 표시된다.
"""
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from cif.config import Config
from cif.ingest.loader import missing_columns, read_table
from cif.ingest.mapping import Issue, StatementRow, normalize_rows

MIN_PERIODS = 3


class ValidationResult(BaseModel):
    ok: bool
    company_code: str | None
    scope: str | None
    rows: list[StatementRow]
    issues: list[Issue]

    def values(self) -> dict[int, dict[str, int]]:
        """연도 → 표준계정코드 → 원 단위 금액 (재무비율 엔진 입력)."""
        out: dict[int, dict[str, int]] = defaultdict(dict)
        for r in self.rows:
            out[r.fiscal_year][r.account_code] = r.amount_krw
        return dict(out)


def _issue(level, code, message, **kw) -> Issue:
    return Issue(level=level, code=code, message=message, **kw)


def validate(df: pd.DataFrame, cfg: Config) -> ValidationResult:
    cols = missing_columns(df)
    if cols:
        return ValidationResult(ok=False, company_code=None, scope=None, rows=[], issues=[
            _issue("ERROR", "MISSING_COLUMNS", f"필수 컬럼 누락: {', '.join(cols)}")])
    if df.empty:
        return ValidationResult(ok=False, company_code=None, scope=None, rows=[], issues=[
            _issue("ERROR", "EMPTY_FILE", "데이터 행이 없습니다")])

    rows, issues = normalize_rows(df, cfg)

    companies = sorted({r.company_code for r in rows})
    if len(companies) > 1:
        issues.append(_issue("ERROR", "MULTIPLE_COMPANIES", f"한 파일에 여러 기업이 있습니다: {companies}"))
    company = companies[0] if len(companies) == 1 else None

    # 범위 선택: 우선순위가 가장 높은 범위 하나만 사용, 나머지는 제외
    scopes = {r.scope for r in rows}
    scope = next((s for s in cfg.settings.scope_priority if s in scopes), None)
    if len(scopes) > 1:
        dropped = [r for r in rows if r.scope != scope]
        issues.append(_issue("WARNING", "SCOPE_MIXED",
                             f"연결/별도 재무제표가 섞여 있어 {scope}만 사용하고 {len(dropped)}행을 제외했습니다"))
        missing_years = {r.fiscal_year for r in dropped} - {r.fiscal_year for r in rows if r.scope == scope}
        for y in sorted(missing_years):
            issues.append(_issue("WARNING", "SCOPE_YEAR_GAP", f"{y}년은 {scope} 자료가 없어 분석에서 빠집니다",
                                 fiscal_year=y))
        rows = [r for r in rows if r.scope == scope]

    # 결산일: 같은 연도 내 결산일 불일치는 오류, 연도 간 결산월 변경은 경고
    dates = defaultdict(set)
    for r in rows:
        dates[r.fiscal_year].add(r.fiscal_end_date)
    for y, ds in sorted(dates.items()):
        if len(ds) > 1:
            issues.append(_issue("ERROR", "FISCAL_DATE_CONFLICT",
                                 f"{y}년에 서로 다른 결산일이 있습니다: {sorted(map(str, ds))}", fiscal_year=y))
    months = {y: next(iter(ds)).month for y, ds in dates.items() if len(ds) == 1}
    if len(set(months.values())) > 1:
        issues.append(_issue("WARNING", "FISCAL_MONTH_CHANGED",
                             f"결산월이 연도마다 다릅니다 {dict(sorted(months.items()))}: 연도 간 비교에 주의"))

    if len({r.raw_unit for r in rows}) > 1:
        issues.append(_issue("INFO", "UNIT_MIXED",
                             f"단위가 섞여 있어 원 단위로 환산했습니다: {sorted({r.raw_unit for r in rows})}"))

    # 중복: 같은 (연도, 계정) 값이 같으면 1건만 유지, 다르면 어느 값도 쓰지 않고 오류
    groups = defaultdict(list)
    for r in rows:
        groups[(r.fiscal_year, r.account_code)].append(r)
    kept = []
    for (y, code), rs in groups.items():
        src = [r.source_row for r in rs]
        if len({r.amount_krw for r in rs}) > 1:
            issues.append(_issue("ERROR", "DUPLICATE_CONFLICT", f"같은 계정에 서로 다른 금액 (행 {src})",
                                 fiscal_year=y, account_code=code))
            continue
        if len(rs) > 1:
            issues.append(_issue("WARNING", "DUPLICATE_SAME", f"같은 값의 중복 행을 1건으로 처리 (행 {src})",
                                 fiscal_year=y, account_code=code))
        kept.append(rs[0])
    rows = sorted(kept, key=lambda r: (r.fiscal_year, r.account_code))

    by_year = defaultdict(dict)
    for r in rows:
        by_year[r.fiscal_year][r.account_code] = r.amount_krw
    for y, v in sorted(by_year.items()):
        for code in cfg.accounts.required:
            if code not in v:
                issues.append(_issue("WARNING", "MISSING_REQUIRED", f"{y}년 필수 계정 누락: {code}",
                                     fiscal_year=y, account_code=code))
        for code in cfg.accounts.non_negative:
            if v.get(code, 0) < 0:
                issues.append(_issue("WARNING", "NEGATIVE_VALUE", f"{y}년 {code}가 음수입니다: {v[code]:,}원",
                                     fiscal_year=y, account_code=code))
        issues += _consistency(y, v, cfg.settings.balance_tolerance)
        if y - 1 in by_year:
            issues += _equity_rollforward(y, by_year[y - 1], v, cfg.settings.equity_rollforward_tolerance)

    if 0 < len(by_year) < MIN_PERIODS:
        issues.append(_issue("INFO", "INSUFFICIENT_PERIODS",
                             f"분석 가능 연도가 {len(by_year)}개로 {MIN_PERIODS}개년 추세 분석이 제한됩니다"))

    ok = not any(i.level == "ERROR" for i in issues) and bool(rows)
    if not rows and not any(i.level == "ERROR" for i in issues):
        issues.append(_issue("ERROR", "NO_USABLE_ROWS", "분석에 사용할 수 있는 행이 없습니다"))
    return ValidationResult(ok=ok, company_code=company, scope=scope, rows=rows, issues=issues)


def _consistency(y: int, v: dict[str, int], tol: float) -> list[Issue]:
    out = []
    ta, tl, te = v.get("TOTAL_ASSETS"), v.get("TOTAL_LIABILITIES"), v.get("TOTAL_EQUITY")
    if None not in (ta, tl, te) and abs(ta - tl - te) > tol * abs(ta):
        out.append(_issue("WARNING", "BALANCE_MISMATCH",
                          f"{y}년 자산({ta:,}) ≠ 부채({tl:,}) + 자본({te:,}), 차이 {ta - tl - te:,}원", fiscal_year=y))
    for part, total in (("CURRENT_ASSETS", "TOTAL_ASSETS"), ("CURRENT_LIABILITIES", "TOTAL_LIABILITIES"),
                        ("INVENTORIES", "CURRENT_ASSETS")):
        if part in v and total in v and v[part] > v[total]:
            out.append(_issue("WARNING", "PART_EXCEEDS_TOTAL", f"{y}년 {part}가 {total}보다 큽니다",
                              fiscal_year=y, account_code=part))
    return out


def _equity_rollforward(y: int, prev: dict[str, int], cur: dict[str, int], tol: float) -> list[Issue]:
    """자본 변동과 당기순이익이 크게 다르면 경고 (배당·증자·기타포괄손익 등 다른 자본 변동 확인 필요)."""
    te0, te1, ni = prev.get("TOTAL_EQUITY"), cur.get("TOTAL_EQUITY"), cur.get("NET_INCOME")
    if None in (te0, te1, ni):
        return []
    gap = (te1 - te0) - ni
    if abs(gap) <= tol * max(abs(te0), abs(ni)):
        return []
    return [_issue("WARNING", "EQUITY_ROLLFORWARD_GAP",
                   f"{y}년 자본 변동 {te1 - te0:,}원과 당기순이익 {ni:,}원의 차이 {gap:,}원 "
                   f"(배당·증자·기타포괄손익 등 확인 필요)", fiscal_year=y, account_code="TOTAL_EQUITY")]


def validate_file(path: Path | str, cfg: Config) -> ValidationResult:
    try:
        df = read_table(path)
    except (ValueError, OSError, zipfile.BadZipFile) as e:  # 손상된 XLSX는 BadZipFile
        return ValidationResult(ok=False, company_code=None, scope=None, rows=[], issues=[
            _issue("ERROR", "UNREADABLE_FILE", str(e))])
    return validate(df, cfg)
