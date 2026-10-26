import pandas as pd
import pytest

from cif.analysis.ratios import compute_ratios
from cif.config import load_config
from cif.db import repository as repo
from cif.ingest.validation import validate, validate_file
from cif.sample_data import generate, write_samples


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    write_samples(d)
    return d


def _codes(result, level=None):
    return {i.code for i in result.issues if level is None or i.level == level}


def _clean():
    return generate()["DEMO01"].astype(str)


def test_clean_sample_has_no_issues(cfg, samples):
    r = validate_file(samples / "DEMO01.csv", cfg)
    assert r.ok and r.scope == "CFS" and r.company_code == "DEMO01"
    assert r.issues == []
    assert sorted(r.values()) == [2023, 2024, 2025]


def test_csv_and_xlsx_give_same_result(cfg, samples):
    for code in ("DEMO01", "DEMO04"):
        a = validate_file(samples / f"{code}.csv", cfg)
        b = validate_file(samples / f"{code}.xlsx", cfg)
        assert a.values() == b.values()
        assert [i.code for i in a.issues] == [i.code for i in b.issues]


def test_dirty_sample(cfg, samples):
    r = validate_file(samples / "DEMO04.csv", cfg)
    assert r.ok  # 경고만 있고 오류는 없음
    assert {"SCOPE_MIXED", "DUPLICATE_SAME", "MISSING_REQUIRED", "UNIT_MIXED"} <= _codes(r)
    assert r.scope == "CFS" and all(row.scope == "CFS" for row in r.rows)
    assert "INVENTORIES" not in r.values()[2024]


def test_mixed_units_same_value(cfg):
    df = _clean()
    first = df[df["fiscal_year"] == "2023"].index
    df.loc[first, "amount"] = (df.loc[first, "amount"].astype(int) * 1_000_000).astype(str)
    df.loc[first, "unit"] = "원"
    a, b = validate(_clean(), cfg), validate(df, cfg)
    assert a.values() == b.values() and "UNIT_MIXED" in _codes(b)


def test_duplicate_conflict_is_error(cfg):
    df = _clean()
    dup = df.iloc[[0]].assign(amount="1")
    r = validate(pd.concat([df, dup], ignore_index=True), cfg)
    assert not r.ok and "DUPLICATE_CONFLICT" in _codes(r, "ERROR")
    assert "TOTAL_ASSETS" not in r.values()[2023]  # 어느 값도 채택하지 않음


def test_synonym_duplicate_detected(cfg):
    df = _clean()
    syn = df.iloc[[0]].assign(account_name="총자산")  # 자산총계와 같은 계정, 같은 값
    assert "DUPLICATE_SAME" in _codes(validate(pd.concat([df, syn], ignore_index=True), cfg))


def test_ofs_only_file_uses_ofs(cfg):
    r = validate(_clean().assign(statement_scope="OFS"), cfg)
    assert r.ok and r.scope == "OFS" and "SCOPE_MIXED" not in _codes(r)


def test_scope_year_gap(cfg):
    df = _clean()
    df.loc[df["fiscal_year"] == "2023", "statement_scope"] = "OFS"
    r = validate(df, cfg)
    assert r.scope == "CFS" and sorted(r.values()) == [2024, 2025]
    assert {"SCOPE_MIXED", "SCOPE_YEAR_GAP", "INSUFFICIENT_PERIODS"} <= _codes(r)


def test_fiscal_month_changed(cfg):
    df = _clean()
    df.loc[df["fiscal_year"] == "2025", "fiscal_end_date"] = "2025-03-31"
    r = validate(df, cfg)
    assert r.ok and "FISCAL_MONTH_CHANGED" in _codes(r, "WARNING")


def test_conflicting_end_dates_in_same_year(cfg):
    df = _clean()
    df.loc[df.index[0], "fiscal_end_date"] = "2023-06-30"
    r = validate(df, cfg)
    assert not r.ok and "FISCAL_DATE_CONFLICT" in _codes(r, "ERROR")


def test_multiple_companies(cfg):
    df = _clean()
    df.loc[df.index[0], "company_code"] = "OTHER"
    assert "MULTIPLE_COMPANIES" in _codes(validate(df, cfg), "ERROR")


def test_missing_required_account(cfg):
    df = _clean()
    df = df[~((df["fiscal_year"] == "2025") & (df["account_name"] == "자본금"))]
    r = validate(df, cfg)
    assert r.ok and any(i.code == "MISSING_REQUIRED" and i.account_code == "PAID_IN_CAPITAL" and i.fiscal_year == 2025
                        for i in r.issues)


def test_balance_mismatch_and_negative(cfg):
    df = _clean()
    m = (df["fiscal_year"] == "2025") & (df["account_name"] == "자산총계")
    df.loc[m, "amount"] = "999999"
    m = (df["fiscal_year"] == "2024") & (df["account_name"] == "이자비용")
    df.loc[m, "amount"] = "-5"
    r = validate(df, cfg)
    assert {"BALANCE_MISMATCH", "NEGATIVE_VALUE"} <= _codes(r, "WARNING")


def _shift_equity(df, year: str, delta_million: int):
    """자산 = 부채 + 자본은 유지하면서 자본만 옮긴다 (부채에서 같은 금액을 뺌)."""
    df = df.copy()
    for account, sign in (("자본총계", 1), ("부채총계", -1)):
        m = (df["fiscal_year"] == year) & (df["account_name"] == account)
        df.loc[m, "amount"] = (df.loc[m, "amount"].astype(int) + sign * delta_million).astype(str)
    return df


def test_equity_rollforward_gap_flagged(cfg):
    # 2025년 자본만 30억 더 늘림: 순이익으로 설명되지 않는 자본 변동
    r = validate(_shift_equity(_clean(), "2025", 3_000), cfg)
    gaps = [i for i in r.issues if i.code == "EQUITY_ROLLFORWARD_GAP"]
    assert r.ok and [i.fiscal_year for i in gaps] == [2025] and gaps[0].level == "WARNING"
    assert "BALANCE_MISMATCH" not in _codes(r)


def test_equity_rollforward_within_tolerance(cfg):
    # 기초 자본(약 330억)의 1% 미만 차이 = 소액 배당 수준은 경고하지 않음
    r = validate(_shift_equity(_clean(), "2025", -100), cfg)
    assert "EQUITY_ROLLFORWARD_GAP" not in _codes(r)


def test_equity_rollforward_needs_both_years_and_net_income(cfg):
    df = _shift_equity(_clean(), "2025", 3_000)
    no_ni = df[~((df["fiscal_year"] == "2025") & (df["account_name"] == "당기순이익"))]
    assert "EQUITY_ROLLFORWARD_GAP" not in _codes(validate(no_ni, cfg))
    gap_year = df[df["fiscal_year"] != "2024"]  # 2023 → 2025는 연속 연도가 아님
    assert "EQUITY_ROLLFORWARD_GAP" not in _codes(validate(gap_year, cfg))


def test_row_error_blocks_analysis(cfg):
    df = _clean()
    df.loc[df.index[3], "amount"] = "N/A"
    r = validate(df, cfg)
    assert not r.ok and "INVALID_AMOUNT" in _codes(r)


@pytest.mark.parametrize("df,code", [
    (pd.DataFrame({"company_code": ["A"]}), "MISSING_COLUMNS"),
    (_clean().iloc[0:0], "EMPTY_FILE"),
    (_clean().assign(account_name="모르는계정"), "NO_USABLE_ROWS"),
])
def test_unusable_input(cfg, df, code):
    r = validate(df, cfg)
    assert not r.ok and code in _codes(r, "ERROR")


def test_unreadable_files(cfg, tmp_path):
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "bad.xlsx").write_bytes(b"not a zip")
    for name in ("x.txt", "bad.xlsx", "missing.csv"):
        assert "UNREADABLE_FILE" in _codes(validate_file(tmp_path / name, cfg))


def test_cp949_csv(cfg, tmp_path):
    p = tmp_path / "cp949.csv"
    _clean().to_csv(p, index=False, encoding="cp949")
    assert validate_file(p, cfg).values() == validate(_clean(), cfg).values()


def test_validated_rows_fit_db_schema(cfg, samples, tmp_path):
    r = validate_file(samples / "DEMO04.csv", cfg)
    conn = repo.connect(tmp_path / "t.db")
    cid = repo.create_company(conn, "DEMO04", "가상건설(주)")
    doc_id = repo.store_document(conn, cid, samples / "DEMO04.csv", tmp_path / "raw")
    rows = [{**row.model_dump(exclude={"company_code"}), "fiscal_end_date": str(row.fiscal_end_date),
             "company_id": cid, "document_id": doc_id} for row in r.rows]
    assert repo.insert_statements(conn, rows) == len(repo.get_statements(conn, doc_id, "CFS"))
    conn.close()


def test_samples_end_to_end_ratios(cfg, samples):
    """검증 → 비율 계산 연결 확인: 자본잠식 기업의 부채비율은 계산하지 않는다."""
    ratios = compute_ratios(validate_file(samples / "DEMO03.csv", cfg).values())
    debt_2025 = next(r for r in ratios if r.code == "DEBT_RATIO" and r.fiscal_year == 2025)
    assert debt_2025.status == "NOT_MEANINGFUL"
    demo04 = compute_ratios(validate_file(samples / "DEMO04.csv", cfg).values())
    quick_2024 = next(r for r in demo04 if r.code == "QUICK_RATIO" and r.fiscal_year == 2024)
    assert quick_2024.status == "MISSING_INPUT"
