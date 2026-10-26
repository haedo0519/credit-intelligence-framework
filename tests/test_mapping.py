import pandas as pd
import pytest

from cif.config import load_config
from cif.ingest.mapping import normalize_rows, parse_amount


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _df(*rows):
    base = dict(company_code="T01", fiscal_year="2025", fiscal_end_date="2025-12-31",
                statement_scope="CFS", account_name="매출액", amount="1", unit="원")
    return pd.DataFrame([{**base, **r} for r in rows])


def test_units_normalized_to_won(cfg):
    rows, issues = normalize_rows(_df(
        dict(amount="5000000", unit="원"),
        dict(amount="5,000", unit="천원", account_name="영업이익"),
        dict(amount="5", unit="백만원", account_name="당기순이익"),
    ), cfg)
    assert issues == []
    assert [r.amount_krw for r in rows] == [5_000_000] * 3


def test_decimal_amount_no_float_error(cfg):
    rows, _ = normalize_rows(_df(dict(amount="0.1", unit="천원"), dict(amount="1.234567", unit="백만원",
                                                                    account_name="자산총계")), cfg)
    assert [r.amount_krw for r in rows] == [100, 1_234_567]


def test_sub_won_rounding_warned(cfg):
    rows, issues = normalize_rows(_df(dict(amount="10.5", unit="원")), cfg)
    assert rows[0].amount_krw == 11  # 사사오입
    assert [i.code for i in issues] == ["ROUNDED_TO_WON"]


@pytest.mark.parametrize("text,expected", [("1,234", 1234), ("(50)", -50), ("-7", -7), (" 12 ", 12),
                                           ("abc", None), ("nan", None), ("inf", None)])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_account_synonyms_and_spaces(cfg):
    rows, _ = normalize_rows(_df(dict(account_name="영업이익(손실)"), dict(account_name="영업활동 현금흐름")), cfg)
    assert [r.account_code for r in rows] == ["OPERATING_INCOME", "OPERATING_CASH_FLOW"]


def test_unmapped_account_excluded_with_warning(cfg):
    rows, issues = normalize_rows(_df(dict(account_name="기타포괄손익")), cfg)
    assert rows == [] and issues[0].code == "UNMAPPED_ACCOUNT" and issues[0].level == "WARNING"


def test_scope_aliases(cfg):
    rows, _ = normalize_rows(_df(dict(statement_scope="연결"), dict(statement_scope="ofs", account_name="자산총계")), cfg)
    assert [r.scope for r in rows] == ["CFS", "OFS"]


@pytest.mark.parametrize("change,code", [
    (dict(unit="억원"), "INVALID_UNIT"),
    (dict(amount=None), "MISSING_AMOUNT"),
    (dict(amount="1백"), "INVALID_AMOUNT"),
    (dict(statement_scope="기타"), "INVALID_SCOPE"),
    (dict(fiscal_year="이천"), "INVALID_YEAR"),
    (dict(fiscal_end_date="2025-13-40"), "INVALID_DATE"),
    (dict(fiscal_end_date="2024-12-31"), "FISCAL_YEAR_MISMATCH"),
    (dict(company_code=None), "MISSING_COMPANY"),
])
def test_row_errors(cfg, change, code):
    rows, issues = normalize_rows(_df(change), cfg)
    assert rows == []
    assert code in [i.code for i in issues] and all(i.level == "ERROR" for i in issues)
    assert issues[0].source_row == 2


def test_raw_values_preserved(cfg):
    rows, _ = normalize_rows(_df(dict(amount="1,000", unit="천원", account_name="매출 액")), cfg)
    r = rows[0]
    assert (r.raw_amount, r.raw_unit, r.raw_account_name, r.amount_krw) == ("1,000", "천원", "매출 액", 1_000_000)


def test_date_formats(cfg):
    rows, _ = normalize_rows(_df(dict(fiscal_end_date="2025.12.31"), dict(fiscal_end_date="2025-12-31 00:00:00",
                                                                        account_name="자산총계")), cfg)
    assert {str(r.fiscal_end_date) for r in rows} == {"2025-12-31"}
