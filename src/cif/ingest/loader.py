"""CSV/XLSX 읽기. 모든 셀을 문자열로 읽어 원본 표기를 그대로 보존한다 (변환은 mapping에서)."""
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = ["company_code", "fiscal_year", "fiscal_end_date", "statement_scope",
                    "account_name", "amount", "unit"]


def read_table(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(path, dtype=str, encoding="cp949")  # 국내 엑셀 기본 저장 인코딩
    elif suffix in (".xlsx", ".xlsm"):
        df = pd.read_excel(path, dtype=str)
    else:
        raise ValueError(f"지원하지 않는 파일 형식: {suffix} (CSV, XLSX만 가능)")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def missing_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in REQUIRED_COLUMNS if c not in df.columns]
