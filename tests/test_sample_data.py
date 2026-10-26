import pandas as pd

from cif.db.repository import file_sha256
from cif.sample_data import COLUMNS, COMPANIES, generate, write_samples


def test_same_seed_same_files(tmp_path):
    a = write_samples(tmp_path / "a", seed=7)
    b = write_samples(tmp_path / "b", seed=7)
    csvs = [(x, y) for x, y in zip(a, b) if x.suffix == ".csv"]
    assert csvs and all(file_sha256(x) == file_sha256(y) for x, y in csvs)


def test_different_seed_differs():
    assert not generate(1)["DEMO01"].equals(generate(2)["DEMO01"])


def test_xlsx_matches_csv(tmp_path):
    write_samples(tmp_path)
    for code in COMPANIES:
        csv = pd.read_csv(tmp_path / f"{code}.csv", encoding="utf-8-sig")
        xlsx = pd.read_excel(tmp_path / f"{code}.xlsx")
        pd.testing.assert_frame_equal(csv, xlsx, check_dtype=False)
        assert list(csv.columns) == COLUMNS


def _pivot(df):
    return df.pivot_table(index="fiscal_year", columns="account_name", values="amount", aggfunc="first")


def test_balance_identity_for_clean_companies():
    data = generate()
    for code in ("DEMO01", "DEMO02", "DEMO03"):
        p = _pivot(data[code])
        diff = (p["자산총계"] - p["부채총계"] - p["자본총계"]).abs()
        assert (diff <= 1).all(), code  # 백만원 반올림 오차만 허용


def test_equity_rolls_forward_by_net_income():
    """자본 변동 = 당기순이익 (실제 Claude 보고서가 이전 데이터의 불일치를 지적해 추가한 검사)."""
    data = generate()
    for code in ("DEMO01", "DEMO02", "DEMO03"):
        p = _pivot(data[code])
        for prev, cur in ((2023, 2024), (2024, 2025)):
            assert p.loc[cur, "자본총계"] - p.loc[prev, "자본총계"] == p.loc[cur, "당기순이익"], (code, cur)


def test_scenario_profiles():
    data = generate()
    loss = _pivot(data["DEMO02"])["영업이익"]
    assert (loss.loc[[2024, 2025]] < 0).all()
    eq = _pivot(data["DEMO03"])
    assert eq.loc[2025, "자본총계"] <= 0 < eq.loc[2024, "자본총계"] < eq.loc[2024, "자본금"]


def test_dirty_company_has_defects():
    df = generate()["DEMO04"]
    assert set(df["unit"]) == {"천원", "백만원"}
    assert set(df["statement_scope"]) == {"CFS", "OFS"}
    cfs = df[df["statement_scope"] == "CFS"]
    assert cfs.duplicated(["fiscal_year", "account_name"]).any()
    assert "재고자산" not in set(cfs[cfs["fiscal_year"] == 2024]["account_name"])
