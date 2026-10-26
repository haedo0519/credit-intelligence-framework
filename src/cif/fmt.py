"""보고서 문장용 금액 표기 (진단·보고서 생성기 공용)."""


def won(v: float) -> str:
    """금액 크기에 맞는 단위: 1조원 이상은 조원, 1억원 이상은 억원, 그 미만은 백만원."""
    if abs(v) >= 1e12:
        return f"{v / 1e12:,.2f}조원"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.1f}억원"
    return f"{v / 1e6:,.0f}백만원"
