"""행 단위 정규화: 계정과목 표준 매핑, 금액 파싱, 단위 → 원 환산, 결산일 검증."""
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

import pandas as pd
from pydantic import BaseModel

from cif.config import Config, normalize_account_name

Level = Literal["ERROR", "WARNING", "INFO"]
SCOPE_ALIASES = {"CFS": "CFS", "OFS": "OFS", "연결": "CFS", "별도": "OFS"}


class Issue(BaseModel):
    level: Level
    code: str
    message: str
    source_row: int | None = None
    fiscal_year: int | None = None
    account_code: str | None = None


class StatementRow(BaseModel):
    company_code: str
    fiscal_year: int
    fiscal_end_date: date
    scope: Literal["CFS", "OFS"]
    account_code: str
    amount_krw: int
    raw_account_name: str
    raw_amount: str
    raw_unit: str
    source_row: int


def _cell(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def parse_amount(text: str) -> Decimal | None:
    """'1,234', '-50', '(50)'(회계식 음수) 허용. 해석 불가면 None."""
    s = text.replace(",", "").replace(" ", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def parse_date(text: str) -> date | None:
    s = text.replace(".", "-").replace("/", "-")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def normalize_rows(df: pd.DataFrame, cfg: Config) -> tuple[list[StatementRow], list[Issue]]:
    lookup = cfg.accounts.lookup()
    units = cfg.settings.units
    rows, issues = [], []
    for idx, rec in enumerate(df.to_dict("records")):
        src = idx + 2  # 파일 기준 행 번호 (1행은 헤더)
        c = {k: _cell(rec.get(k)) for k in rec}
        errs = []

        def err(code, msg):
            errs.append(Issue(level="ERROR", code=code, message=msg, source_row=src))

        if not c.get("company_code"):
            err("MISSING_COMPANY", "기업코드가 비어 있습니다")
        try:
            year = int(Decimal(c.get("fiscal_year", "")))
        except (InvalidOperation, ValueError):
            year = None
            err("INVALID_YEAR", f"결산연도를 해석할 수 없습니다: '{c.get('fiscal_year', '')}'")
        end = parse_date(c.get("fiscal_end_date", ""))
        if end is None:
            err("INVALID_DATE", f"결산일을 해석할 수 없습니다: '{c.get('fiscal_end_date', '')}'")
        elif year is not None and end.year != year:
            err("FISCAL_YEAR_MISMATCH", f"결산연도 {year}와 결산일 {end} 의 연도가 다릅니다")
        scope = SCOPE_ALIASES.get(c.get("statement_scope", "").upper())
        if scope is None:
            err("INVALID_SCOPE", f"재무제표 범위는 CFS/OFS(연결/별도)여야 합니다: '{c.get('statement_scope', '')}'")
        unit = c.get("unit", "")
        if unit not in units:
            err("INVALID_UNIT", f"알 수 없는 단위: '{unit}' (허용: {', '.join(units)})")
        raw_amount = c.get("amount", "")
        amount = parse_amount(raw_amount) if raw_amount else None
        if amount is None:
            err("MISSING_AMOUNT" if not raw_amount else "INVALID_AMOUNT",
                f"금액이 비어 있거나 숫자가 아닙니다: '{raw_amount}'")
        name = c.get("account_name", "")
        code = lookup.get(normalize_account_name(name))
        if code is None and not errs:
            issues.append(Issue(level="WARNING", code="UNMAPPED_ACCOUNT", source_row=src, fiscal_year=year,
                                message=f"표준 계정에 매핑되지 않아 분석에서 제외: '{name}'"))
            continue
        if errs:
            issues += errs
            continue
        krw = amount * units[unit]
        won = krw.to_integral_value(rounding=ROUND_HALF_UP)
        if krw != won:
            issues.append(Issue(level="WARNING", code="ROUNDED_TO_WON", source_row=src, fiscal_year=year,
                                account_code=code, message=f"원 단위 미만 금액을 반올림: {krw}원"))
        rows.append(StatementRow(
            company_code=c["company_code"], fiscal_year=year, fiscal_end_date=end, scope=scope,
            account_code=code, amount_krw=int(won), raw_account_name=name,
            raw_amount=raw_amount, raw_unit=unit, source_row=src,
        ))
    return rows, issues
