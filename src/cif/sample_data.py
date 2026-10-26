"""가상 기업 재무자료 생성기. 실존 기업과 무관하며 시드가 같으면 같은 데이터를 만든다.

출력은 Long 포맷: company_code, fiscal_year, fiscal_end_date, statement_scope, account_name, amount, unit
"""
from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["company_code", "fiscal_year", "fiscal_end_date", "statement_scope",
           "account_name", "amount", "unit"]
YEARS = [2023, 2024, 2025]

# 회사별 기본 프로필 (단위: 백만원). 연도별 값은 아래 _build에서 만든다.
COMPANIES = {
    "DEMO01": {"name": "가상정밀(주)", "industry": "제조", "case": "정상"},
    "DEMO02": {"name": "가상유통(주)", "industry": "도소매", "case": "영업손실 지속"},
    "DEMO03": {"name": "가상바이오(주)", "industry": "바이오", "case": "자본잠식"},
    "DEMO04": {"name": "가상건설(주)", "industry": "건설", "case": "결측·단위혼합·중복·범위혼합"},
}


def _year_values(code: str, i: int, rng: np.random.Generator) -> dict[str, float]:
    """i: 0,1,2 (2023~2025). 반환값은 백만원 단위, 자산 = 부채 + 자본이 성립한다."""
    noise = 1 + rng.uniform(-0.03, 0.03)
    if code == "DEMO01":
        revenue = 50_000 * (1.06 ** i) * noise
        op_income = revenue * 0.09
        equity = 30_000 + 2_500 * i
        assets = 60_000 + 3_000 * i
        interest, ocf, capital = 800, op_income * 1.1, 5_000
    elif code == "DEMO02":
        revenue = 40_000 * (0.9 ** i) * noise
        op_income = [600, -1_200, -2_100][i]
        equity = 12_000 - 1_500 * i
        assets = 45_000 - 1_000 * i
        interest, ocf, capital = 1_500, [900, -400, -1_100][i], 6_000
    elif code == "DEMO03":
        revenue = 3_000 * (1.1 ** i) * noise
        op_income = [-2_500, -3_000, -3_400][i]
        equity = [4_000, 1_500, -800][i]  # 부분잠식 → 완전잠식
        assets = 15_000 - 500 * i
        interest, ocf, capital = 600, [-2_000, -2_600, -3_000][i], 5_000
    else:  # DEMO04
        revenue = 80_000 * (1.02 ** i) * noise
        op_income = revenue * 0.04
        equity = 25_000 + 800 * i
        assets = 90_000 + 2_000 * i
        interest, ocf, capital = 2_000, op_income * 0.7, 10_000
    liabilities = assets - equity
    borrowings = liabilities * 0.55
    return {
        "자산총계": assets,
        "유동자산": assets * 0.45,
        "재고자산": assets * 0.12,
        "부채총계": liabilities,
        "유동부채": liabilities * 0.6,
        "자본총계": equity,
        "자본금": capital,
        "매출액": revenue,
        "영업이익": op_income,
        "당기순이익": op_income * 0.75 - interest * 0.5,
        "이자비용": interest,
        "영업활동현금흐름": ocf,
        "차입금": borrowings,
    }


def _rows(code: str, rng: np.random.Generator) -> list[dict]:
    rows = []
    for i, year in enumerate(YEARS):
        for account, value in _year_values(code, i, rng).items():
            rows.append({
                "company_code": code, "fiscal_year": year, "fiscal_end_date": f"{year}-12-31",
                "statement_scope": "CFS", "account_name": account,
                "amount": int(round(value)), "unit": "백만원",
            })
    if code == "DEMO04":
        rows = _make_dirty(rows)
    return rows


def _make_dirty(rows: list[dict]) -> list[dict]:
    """검증 테스트용 결함 주입. 수치 자체는 단위 환산 후 동일하게 유지한다."""
    out = []
    for r in rows:
        if r["fiscal_year"] == 2024 and r["account_name"] == "재고자산":
            continue  # 결측
        if r["fiscal_year"] == 2023:
            r = {**r, "amount": r["amount"] * 1000, "unit": "천원"}  # 단위 혼합
        out.append(r)
        if r["fiscal_year"] == 2025 and r["account_name"] == "매출액":
            out.append(dict(r))  # 같은 값 중복
    # 같은 연도에 별도재무제표 일부 혼입
    out += [{**r, "statement_scope": "OFS", "amount": int(r["amount"] * 0.9)}
            for r in rows if r["fiscal_year"] == 2025 and r["account_name"] in ("자산총계", "매출액")]
    return out


def generate(seed: int = 42) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    return {code: pd.DataFrame(_rows(code, rng), columns=COLUMNS) for code in COMPANIES}


def write_samples(out_dir: Path | str, seed: int = 42) -> list[Path]:
    """회사별 CSV(UTF-8 BOM, 엑셀 호환)와 XLSX를 만든다."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for code, df in generate(seed).items():
        csv_path, xlsx_path = out_dir / f"{code}.csv", out_dir / f"{code}.xlsx"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig", lineterminator="\n")
        df.to_excel(xlsx_path, index=False)
        paths += [csv_path, xlsx_path]
    return paths


if __name__ == "__main__":
    for p in write_samples(Path(__file__).resolve().parents[2] / "data" / "samples"):
        print(p)
