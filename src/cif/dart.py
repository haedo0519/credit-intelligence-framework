"""OpenDART에서 상장사 재무제표를 받아 CIF 입력 형식(CSV)으로 저장한다.

    python -m cif.dart --company 삼성전자 --years 2022 2023 2024 --env-file <.env 경로>

인증키는 DART_API_KEY 환경변수 또는 --env-file의 DART_API_KEY 줄에서 실행 시점에만 읽는다.
키는 화면·로그·파일·예외 메시지에 남기지 않는다. 받은 실제 기업 자료는 git에서 제외된 data/real/에 둔다.
"""
import argparse
import calendar
import csv
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import yaml

from cif.config import CONFIG_DIR, load_config, normalize_account_name

API = "https://opendart.fss.or.kr/api"
ANNUAL_REPORT = "11011"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / "data" / "dart_cache"
STATUS = {"010": "등록되지 않은 인증키", "011": "사용할 수 없는 인증키", "012": "접근할 수 없는 IP",
          "013": "조회된 데이터가 없음", "020": "요청 제한 초과", "100": "필드 값 오류", "800": "시스템 점검 중",
          "901": "인증키 기간 만료"}


class DartError(Exception):
    """메시지에 인증키나 요청 URL을 넣지 않는다."""


def load_key(env_file: Path | None = None) -> str:
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key and env_file is not None:
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip().removeprefix("export ").strip() == "DART_API_KEY":
                key = value.split(" #")[0].strip().strip("'\"")
                break
    if not key:
        raise DartError("DART_API_KEY를 찾지 못했습니다 (환경변수 또는 --env-file)")
    return key


def _get(endpoint: str, params: dict, key: str) -> bytes:
    url = f"{API}/{endpoint}?" + urllib.parse.urlencode({"crtfc_key": key, **params})
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise DartError(f"DART 요청 실패 (HTTP {e.code}, {endpoint})") from None  # 원 예외에는 키가 든 URL이 있음
    except (urllib.error.URLError, TimeoutError) as e:
        raise DartError(f"DART 연결 실패 ({endpoint}): {getattr(e, 'reason', e)}") from None


def _json(endpoint: str, params: dict, key: str) -> dict:
    data = json.loads(_get(endpoint, params, key))
    status = data.get("status")
    if status != "000":
        raise DartError(f"DART 오류 {status}: {STATUS.get(status, data.get('message', ''))} ({endpoint})")
    return data


def find_corp(name_or_stock: str, key: str, cache_dir: Path = CACHE_DIR) -> dict:
    """회사명(정확히 일치) 또는 6자리 종목코드로 고유번호를 찾는다. 상장사를 우선한다."""
    cache = cache_dir / "corpCode.xml"
    if not cache.exists():
        raw = _get("corpCode.xml", {}, key)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read(z.namelist()[0])
        except zipfile.BadZipFile:
            raise DartError("고유번호 목록을 받지 못했습니다 (인증키 확인)") from None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(xml)
    q = name_or_stock.strip()
    found = []
    for el in ElementTree.parse(cache).getroot().iter("list"):
        item = {k: (el.findtext(k) or "").strip() for k in ("corp_code", "corp_name", "stock_code")}
        if q in (item["corp_name"], item["stock_code"]):
            found.append(item)
    found.sort(key=lambda i: i["stock_code"] == "")
    if not found:
        raise DartError(f"'{q}'에 해당하는 회사를 찾지 못했습니다 (정확한 회사명 또는 종목코드)")
    return found[0]


def load_map(path: Path = CONFIG_DIR / "dart_map.yaml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _norm(name: str) -> str:
    # "Ⅰ. 영업활동현금흐름", "1.유동자산" 같은 번호 접두어 제거
    return normalize_account_name(re.sub(r"^\s*[\dⅠ-ⅩIVX]+\s*[.)]\s*", "", name))


def _amount(text: str | None) -> int | None:
    s = (text or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def map_statements(items: list[dict], mapping: dict) -> tuple[dict[str, int], list[str]]:
    """DART 항목 → {표준계정코드: 원 단위 금액}, 차입금 합산에 쓴 계정명 목록."""
    for it in items:
        if it.get("currency", "KRW") not in ("KRW", ""):
            raise DartError(f"원화가 아닌 재무제표는 지원하지 않습니다 ({it.get('currency')})")
    out: dict[str, int] = {}
    for code, spec in mapping["accounts"].items():
        names = {_norm(n) for n in spec["names"]}
        # name_check: 회사가 표준코드를 잘못 붙인 경우를 거르기 위해 코드로 찾은 항목의 이름도 확인
        name_ok = lambda it: not spec.get("name_check") or re.search(spec["name_check"], it.get("account_nm", ""))  # noqa: E731
        for sj in spec["sj"]:
            rows = [it for it in items if it.get("sj_div") == sj]
            hit = next((it for it in rows if it.get("account_id") in spec["ids"] and name_ok(it)), None) \
                or next((it for it in rows if _norm(it.get("account_nm", "")) in names), None)
            if hit is not None and _amount(hit.get("thstrm_amount")) is not None:
                out[code] = _amount(hit["thstrm_amount"])
                break
    b = mapping["borrowings"]
    used, total = [], 0
    for it in items:
        name = _norm(it.get("account_nm", ""))
        if it.get("sj_div") in b["sj"] and re.search(b["name_pattern"], name) \
                and not re.search(b["exclude_pattern"], name) and _amount(it.get("thstrm_amount")) is not None:
            used.append(it["account_nm"])
            total += _amount(it["thstrm_amount"])
    if used:
        out["BORROWINGS"] = total
    return out, used


def fiscal_end(year: int, month: int) -> str:
    return f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def to_rows(company_code: str, year: int, end_date: str, scope: str, values: dict[str, int]) -> list[dict]:
    labels = {code: names[0] for code, names in load_config().accounts.accounts.items()}
    return [{"company_code": company_code, "fiscal_year": year, "fiscal_end_date": end_date,
             "statement_scope": scope, "account_name": labels[code], "amount": amount, "unit": "원"}
            for code, amount in values.items()]


def import_company(query: str, years: list[int], scope: str, key: str, out_dir: Path) -> Path:
    corp = find_corp(query, key)
    info = _json("company.json", {"corp_code": corp["corp_code"]}, key)
    month = int(info.get("acc_mt") or 12)
    code = corp["stock_code"] or corp["corp_code"]
    mapping = load_map()
    rows = []
    for y in years:
        data = _json("fnlttSinglAcntAll.json", {"corp_code": corp["corp_code"], "bsns_year": str(y),
                                                "reprt_code": ANNUAL_REPORT, "fs_div": scope}, key)
        values, used = map_statements(data["list"], mapping)
        missing = [c for c in mapping["accounts"] if c not in values]
        print(f"  {y}: 계정 {len(values)}개" + (f", 없음: {', '.join(missing)}" if missing else "")
              + (f", 차입금 합산: {', '.join(used)}" if used else ""))
        rows += to_rows(code, y, fiscal_end(y, month), scope, values)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{code}_{scope}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{corp['corp_name']} ({code}, 결산월 {month}월) → {path}")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenDART 재무제표 → CIF 입력 CSV")
    ap.add_argument("--company", required=True, action="append", help="회사명(정확히) 또는 종목코드. 여러 번 지정 가능")
    ap.add_argument("--years", required=True, type=int, nargs="+")
    ap.add_argument("--scope", choices=["CFS", "OFS"], default="CFS")
    ap.add_argument("--env-file", type=Path, help="DART_API_KEY가 있는 .env 파일")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "real")
    args = ap.parse_args(argv)
    key = load_key(args.env_file)
    for company in args.company:
        try:
            import_company(company, sorted(set(args.years)), args.scope, key, args.out)
        except DartError as e:
            print(f"{company}: {e}")


if __name__ == "__main__":
    main()
