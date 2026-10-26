"""OpenDART 가져오기 테스트. 네트워크를 쓰지 않고 DART 응답 형식의 가짜 데이터를 사용한다."""
import io
import json
import urllib.error
import zipfile

import pytest

from cif import dart
from cif.config import load_config
from cif.ingest.validation import validate_file

FAKE_KEY = "test-key-0123456789abcdef0123456789abcd"


def item(sj, acc_id, name, amount, currency="KRW"):
    return {"sj_div": sj, "account_id": acc_id, "account_nm": name, "thstrm_amount": amount, "currency": currency}


def statements(scale=1):
    """한 해 치 DART 항목 (원 단위). scale로 연도별 값을 다르게 만든다."""
    a = lambda v: str(v * scale)  # noqa: E731
    return [
        item("BS", "ifrs-full_CurrentAssets", "유동자산", a(400)),
        item("BS", "ifrs-full_Inventories", "재고자산", a(100)),
        item("BS", "ifrs-full_Assets", "자산총계", a(1000)),
        item("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", a(50)),
        item("BS", "-표준계정코드 미사용-", "유동성장기차입금", a(20)),
        item("BS", "-표준계정코드 미사용-", "사채", a(100)),
        item("BS", "-표준계정코드 미사용-", "사채할인발행차금", a(-3)),   # 차입금 아님
        item("BS", "ifrs-full_LeaseLiabilities", "리스부채", a(30)),        # 차입금 아님
        item("BS", "ifrs-full_CurrentLiabilities", "유동부채", a(200)),
        item("BS", "ifrs-full_Liabilities", "부채총계", a(600)),
        item("BS", "ifrs-full_IssuedCapital", "자본금", a(100)),
        item("BS", "ifrs-full_Equity", "자본총계", a(400)),
        item("IS", "ifrs-full_Revenue", "매출액", a(2000)),
        item("IS", "dart_OperatingIncomeLoss", "영업이익", a(150)),
        item("IS", "ifrs-full_ProfitLoss", "당기순이익", a(80)),
        item("CIS", "ifrs-full_Revenue", "매출액", "999999"),                # IS가 우선
        item("CF", "-표준계정코드 미사용-", "Ⅰ. 영업활동현금흐름", a(120)),  # 이름·번호 접두어로 찾기
    ]


def test_map_statements():
    values, used = dart.map_statements(statements(), dart.load_map())
    assert values["REVENUE"] == 2000                  # IS 우선
    assert values["OPERATING_CASH_FLOW"] == 120       # 계정명으로 대체 매칭
    assert values["BORROWINGS"] == 170                # 단기 50 + 유동성장기 20 + 사채 100
    assert sorted(used) == ["단기차입금", "사채", "유동성장기차입금"]
    assert "INTEREST_EXPENSE" not in values           # 본문에 없으면 지어내지 않음


def test_real_world_variants():
    """현대자동차 DART 응답에서 발견한 형태: 표준코드 없는 '연결당기순이익', 이름이 '부채'로 끝나는 유동성장기부채."""
    items = [item("IS", "-표준계정코드 미사용-", "연결당기순이익", "7983614000000"),
             item("BS", "ifrs-full_CurrentPortionOfLongtermBorrowings", "유동성장기부채", "25574131000000"),
             item("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "11366480000000"),
             item("BS", "ifrs-full_NoncurrentLeaseLiabilities", "리스부채", "986477000000")]
    values, used = dart.map_statements(items, dart.load_map())
    assert values["NET_INCOME"] == 7_983_614_000_000
    assert values["BORROWINGS"] == 25_574_131_000_000 + 11_366_480_000_000
    assert "리스부채" not in used


def test_blank_amounts_skipped_and_currency_checked():
    items = [item("BS", "ifrs-full_Assets", "자산총계", "-"), item("BS", "ifrs-full_Equity", "자본총계", "")]
    assert dart.map_statements(items, dart.load_map())[0] == {}
    with pytest.raises(dart.DartError, match="원화"):
        dart.map_statements([item("BS", "ifrs-full_Assets", "자산총계", "1", currency="USD")], dart.load_map())


def test_load_key_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nexport DART_API_KEY='abc123' # 주석\n", encoding="utf-8")
    assert dart.load_key(env) == "abc123"
    monkeypatch.setenv("DART_API_KEY", "from-env")
    assert dart.load_key(env) == "from-env"           # 환경변수가 우선
    monkeypatch.delenv("DART_API_KEY")
    with pytest.raises(dart.DartError, match="찾지 못했습니다"):
        dart.load_key(None)


def test_errors_never_contain_key(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.HTTPError(url, 500, "err", {}, None)
    monkeypatch.setattr(dart.urllib.request, "urlopen", boom)
    with pytest.raises(dart.DartError) as info:
        dart._get("company.json", {"corp_code": "00126380"}, FAKE_KEY)
    assert FAKE_KEY not in str(info.value) and info.value.__cause__ is None and info.value.__suppress_context__


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_dart(monkeypatch, *, fs_status="000", acc_mt="12"):
    """엔드포인트별 가짜 응답. 요청에 키가 실렸는지도 기록한다."""
    xml = ("<result><list><corp_code>00000001</corp_code><corp_name>가상전자</corp_name><stock_code> </stock_code></list>"
           "<list><corp_code>00000002</corp_code><corp_name>가상전자</corp_name><stock_code>123450</stock_code></list>"
           "</result>").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml)
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        if "corpCode.xml" in url:
            return FakeResp(buf.getvalue())
        if "company.json" in url:
            return FakeResp(json.dumps({"status": "000", "acc_mt": acc_mt}).encode())
        year = int(url.split("bsns_year=")[1][:4])
        body = {"status": fs_status, "message": "x", "list": statements(scale=year - 2020)}
        return FakeResp(json.dumps(body).encode())
    monkeypatch.setattr(dart.urllib.request, "urlopen", urlopen)
    return calls


def test_find_corp_prefers_listed(tmp_path, monkeypatch):
    fake_dart(monkeypatch)
    assert dart.find_corp("가상전자", FAKE_KEY, tmp_path)["corp_code"] == "00000002"
    assert dart.find_corp("123450", FAKE_KEY, tmp_path)["corp_name"] == "가상전자"
    with pytest.raises(dart.DartError, match="찾지 못했습니다"):
        dart.find_corp("없는회사", FAKE_KEY, tmp_path)


def test_import_company_produces_valid_cif_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dart, "CACHE_DIR", tmp_path / "cache")
    fake_dart(monkeypatch, acc_mt="03")
    monkeypatch.setattr(dart, "find_corp", lambda q, key: {"corp_code": "00000002", "corp_name": "가상전자",
                                                          "stock_code": "123450"})
    path = dart.import_company("가상전자", [2022, 2023, 2024], "CFS", FAKE_KEY, tmp_path / "real")
    assert path.name == "123450_CFS.csv"
    v = validate_file(path, load_config())
    assert v.ok and v.scope == "CFS" and sorted(v.values()) == [2022, 2023, 2024]
    assert v.values()[2024]["BORROWINGS"] == 170 * 4
    assert {r.fiscal_end_date.isoformat() for r in v.rows} == {"2022-03-31", "2023-03-31", "2024-03-31"}
    out = capsys.readouterr().out
    assert "없음: INTEREST_EXPENSE" in out and FAKE_KEY not in out


def test_import_reports_dart_status(tmp_path, monkeypatch):
    fake_dart(monkeypatch, fs_status="013")
    monkeypatch.setattr(dart, "find_corp", lambda q, key: {"corp_code": "1", "corp_name": "x", "stock_code": "1"})
    with pytest.raises(dart.DartError, match="013: 조회된 데이터가 없음"):
        dart.import_company("x", [2024], "CFS", FAKE_KEY, tmp_path)


@pytest.mark.parametrize("year,month,expected", [(2024, 2, "2024-02-29"), (2023, 2, "2023-02-28"),
                                                 (2024, 3, "2024-03-31"), (2024, 12, "2024-12-31")])
def test_fiscal_end(year, month, expected):
    assert dart.fiscal_end(year, month) == expected
