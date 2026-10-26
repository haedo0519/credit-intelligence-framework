# CIF (Credit Intelligence Framework) — Phase 0 점검 결과 및 구현 계획

작성일: 2026-10-26 · 상태: Phase 0 (코드 미작성)

> 포트폴리오용 분석 도구. 실제 금융기관 심사 시스템·검증된 신용평가 모델이 아니며, 대출 승인/거절을 결정하지 않는다.

---

## 1. 환경 점검 결과 (실측)

| 항목 | 결과 |
|---|---|
| 작업 디렉터리 | 비어 있음 (기존 파일·의존성 없음). git 저장소 아님 |
| Python | 3.14.7 (64bit, 단일 설치) |
| pip | 26.2.1 |
| SQLite | 3.50.4 (표준 라이브러리 `sqlite3`) |
| git | 2.55.0 설치됨 |
| 이미 설치된 관련 패키지 | Jinja2 3.1.6, plotly 7.0.0 |
| 미설치 | streamlit, pandas, numpy, pydantic, pytest, openpyxl, PyYAML, xhtml2pdf |
| 설치 가능성 | `pip install --dry-run`으로 확인: 위 패키지 전부 Python 3.14용 wheel 존재 (소스 빌드 불필요) |
| PDF 한글 폰트 | `C:\Windows\Fonts\malgun.ttf`, `malgunbd.ttf` 존재 |

해석된 버전(고정 예정): streamlit 1.66.1, pandas 3.1.0, numpy 2.5.4, pydantic 2.14.1, pytest 9.1.1, openpyxl 3.1.5, PyYAML 6.0.3, xhtml2pdf 0.2.28.

**주의:** 현재 폴더는 세션 전용 임시 폴더라 세션 삭제 시 함께 삭제된다. Phase 1 시작 전에 영구 폴더로 옮기고 `git init` 하는 것을 권장.

---

## 2. 요구사항·설계 누락 사항과 제안 결정

명세가 확정하지 않은 금융 기준은 **설정값(YAML) + 가정 문서화**로 분리한다 (원칙 10). 아래 "제안"은 기본값이며 전부 설정에서 바꿀 수 있다.

| # | 누락/모호 사항 | 제안 |
|---|---|---|
| 1 | 입력 파일 형식 미정의 | **Long 포맷** 1종으로 고정: `company_code, fiscal_year, fiscal_end_date, statement_scope(CFS/OFS), account_name, amount, unit`. 계정명 → 표준코드 매핑은 `config/account_map.yaml` |
| 2 | ROA·ROE·총자산회전율의 분모: 기말 vs 평균 | 기본 **평균잔액**((전기말+당기말)/2). 전기 자료가 없으면 기말잔액 사용 + 상태 `FALLBACK_END_BALANCE` 기록 |
| 3 | 이자보상배율의 "이자비용" 범위 | K-IFRS 손익계산서의 금융비용 전체가 아닌 **이자비용 계정**만. 없으면 `MISSING_INPUT` |
| 4 | 영업현금흐름 비율 정의 2종 (OCF/유동부채, OCF/매출) | 기본 **OCF / 유동부채**, 보조지표로 OCF/매출 선택 가능 |
| 5 | 당좌자산 정의 | 유동자산 − 재고자산 (단순 정의, 가정에 명시) |
| 6 | 자본 ≤ 0일 때 부채비율·ROE | 값 계산하지 않고 `NOT_MEANINGFUL`, 자본잠식 규칙이 별도 탐지 |
| 7 | 자본잠식 판정 기준 | 부분잠식: 자본총계 < 자본금, 완전잠식: 자본총계 ≤ 0 → **자본금 계정 필수 입력** |
| 8 | 연결/별도 혼합 처리 | 한 분석 실행(run)에는 **한 범위만** 사용. 기본 CFS 우선, 없으면 OFS. 다른 범위 행은 제외하고 경고(`SCOPE_MIXED`), 선택 범위에 없는 연도는 `SCOPE_YEAR_GAP` *(Phase 2에서 '오류'→'제외+경고'로 변경)* |
| 9 | 결산일이 12/31이 아닌 경우 (3월 결산 등) | `fiscal_year`는 결산일이 속한 연도로 정의. 같은 회사 내 결산월이 바뀌면 경고(비교 불가 연도 표시) |
| 10 | "최근 3개년" 자료 부족 | 1~2개년이면 분석은 진행, 추세 항목은 `INSUFFICIENT_PERIODS`로 보고서 '자료의 한계'에 자동 기재 |
| 11 | 금리 스트레스 가정 | 입력: 총차입금, 변동금리 비중, 금리 변동폭(bp), 적용 개월 수(연환산 비율). 이자비용 증분 = 차입금 × 변동비중 × Δ금리 × (적용개월/12). 차입금 잔액은 기말 기준 일정 가정 |
| 12 | 복합 시나리오의 적용 순서 | 매출 변화 → 변화된 매출에 영업이익률 변화 적용 → 이자비용 변화. 순서를 결과에 기록 |
| 13 | 업종별 기준 부재 | 임계값은 **업종 무관 일반 가정**임을 규칙 파일과 보고서에 명시 (업종 벤치마크는 범위 외) |
| 14 | 금액 저장 정밀도 | DB에는 **원 단위 정수**로 정규화 저장, 비율은 float, 반올림은 표시 단계에서만 |
| 15 | 부호 규칙 | 비용 계정(이자비용 등)은 양수로 입력. 음수면 검증 경고 |
| 16 | 재무제표 정합성 검사 기준 | 자산 = 부채 + 자본 (허용오차 설정값, 기본 0.1%), 유동자산 ≤ 총자산, 유동부채 ≤ 총부채 |
| 17 | LLM 제공자 미정 | `LLMAdapter` 인터페이스 + **MockAdapter(오프라인·결정적)** + 실제 API 어댑터 1종. 키는 환경변수로만 |
| 18 | AI 주장의 근거 검증 방법 | AI는 수치를 직접 쓰지 않고 `evidence_id`(예: `ratio:DEBT_RATIO:2025`)를 참조. 검증기가 ID 존재·수치 일치(허용오차) 확인, 실패한 주장은 "미확인"으로 강등 |
| 19 | "종합 의견"과 자동 결정 금지의 경계 | 스키마에 승인/거절 필드 없음. 출력 텍스트에 "승인", "거절", "대출 가능" 등 결정 표현 탐지 시 검증 실패 |
| 20 | 재현성 기록 대상 | 입력 파일 SHA-256, 규칙 YAML SHA-256, 매핑 YAML 해시, 앱 버전, LLM 모델명·프롬프트 버전·temperature |
| 21 | 분석 이력 불변성 | SQLite 트리거로 `analysis_runs`, `analysis_results`, `audit_logs`의 UPDATE/DELETE 차단 |
| 22 | 시연 데이터 | 실존 기업명 사용 금지. 가상 기업 생성기(시드 고정)로 정상/위험/자본잠식/결측 케이스 생성 |
| 23 | PDF 한글 | xhtml2pdf + 맑은 고딕 등록. 폰트가 없으면 HTML만 출력하고 경고 |
| 24 | 인증·다중 사용자 | 개인 PC 단일 사용자 앱이므로 **범위 외** |

---

## 3. 디렉터리 구조 및 예상 파일 목록

```
cif/
├─ pyproject.toml            # 의존성 고정, pytest 설정
├─ README.md                 # 설치·실행 (Phase 6)
├─ .gitignore                # data/*.db, .env, outputs/
├─ .env.example              # LLM API 키 변수명만
├─ config/
│  ├─ settings.yaml          # 경로, 허용오차, 기본 범위(CFS/OFS)
│  ├─ account_map.yaml       # 계정명 동의어 → 표준코드
│  ├─ risk_rules.yaml        # 위험 규칙 임계값 + version
│  └─ stress_scenarios.yaml  # 기본/스트레스 시나리오 + 가정
├─ src/cif/
│  ├─ __init__.py            # __version__
│  ├─ config.py              # YAML 로드 + Pydantic 검증 + 해시
│  ├─ db/
│  │  ├─ schema.sql          # DDL + 불변성 트리거
│  │  └─ repository.py       # 저장/조회 함수
│  ├─ ingest/
│  │  ├─ loader.py           # CSV/XLSX 읽기, 원본 보존(documents)
│  │  ├─ mapping.py          # 계정 표준화, 단위 정규화
│  │  └─ validation.py       # 결측·중복·이상치·범위·결산일·정합성
│  ├─ analysis/
│  │  ├─ ratios.py           # 10개 비율, MetricResult(status 포함)
│  │  ├─ risk_rules.py       # 규칙 평가
│  │  └─ stress.py           # 시나리오 계산
│  ├─ report/
│  │  ├─ schema.py           # 보고서 Pydantic 모델(10개 섹션)
│  │  ├─ evidence.py         # 근거 패키지 생성 + 주장 검증
│  │  ├─ llm.py              # LLMAdapter, MockAdapter, API 어댑터
│  │  ├─ render.py           # HTML(Jinja2) / PDF
│  │  └─ templates/report.html
│  ├─ pipeline.py            # 업로드→검증→분석→저장 1회 실행(run)
│  └─ sample_data.py         # 가상 데이터 생성기
├─ app/
│  ├─ Home.py
│  └─ pages/  1_기업관리.py 2_데이터검증.py 3_재무분석.py 4_위험분석.py
│             5_시나리오.py 6_보고서.py 7_분석이력.py
├─ tests/
│  ├─ conftest.py            # 임시 DB, 표준 픽스처
│  ├─ test_ratios.py  test_validation.py  test_mapping.py
│  ├─ test_risk_rules.py  test_stress.py
│  ├─ test_report_schema.py  test_llm_failures.py
│  └─ test_db_history.py  test_reproducibility.py
├─ data/                     # cif.db, raw/ (업로드 원본 사본) — git 제외
└─ docs/  assumptions.md  user_manual.md
```

---

## 4. 재무비율 계산 명세

표준 계정코드: `TOTAL_ASSETS, CURRENT_ASSETS, INVENTORIES, TOTAL_LIABILITIES, CURRENT_LIABILITIES, TOTAL_EQUITY, PAID_IN_CAPITAL, REVENUE, OPERATING_INCOME, NET_INCOME, INTEREST_EXPENSE, OPERATING_CASH_FLOW, BORROWINGS`

| 지표 | 계산식 | 사용 계정 | 단위 | 예외 처리 |
|---|---|---|---|---|
| 유동비율 | 유동자산 / 유동부채 × 100 | CURRENT_ASSETS, CURRENT_LIABILITIES | % | 분모 0 → `DIV_BY_ZERO` |
| 당좌비율 | (유동자산 − 재고자산) / 유동부채 × 100 | + INVENTORIES | % | 재고 없음 → `MISSING_INPUT` (0으로 가정하지 않음) |
| 부채비율 | 부채총계 / 자본총계 × 100 | TOTAL_LIABILITIES, TOTAL_EQUITY | % | 자본 ≤ 0 → `NOT_MEANINGFUL` |
| 자기자본비율 | 자본총계 / 자산총계 × 100 | TOTAL_EQUITY, TOTAL_ASSETS | % | 음수 허용(계산함), 자산 0 → `DIV_BY_ZERO` |
| 영업이익률 | 영업이익 / 매출액 × 100 | OPERATING_INCOME, REVENUE | % | 매출 0 → `DIV_BY_ZERO`, 음수 영업이익 허용 |
| ROA | 당기순이익 / 평균자산총계 × 100 | NET_INCOME, TOTAL_ASSETS(당기·전기) | % | 전기 없음 → 기말 사용 `FALLBACK_END_BALANCE` |
| ROE | 당기순이익 / 평균자본총계 × 100 | NET_INCOME, TOTAL_EQUITY(당기·전기) | % | 평균자본 ≤ 0 → `NOT_MEANINGFUL` |
| 총자산회전율 | 매출액 / 평균자산총계 | REVENUE, TOTAL_ASSETS | 회 | ROA와 동일 |
| 이자보상배율 | 영업이익 / 이자비용 | OPERATING_INCOME, INTEREST_EXPENSE | 배 | 이자비용 0 → `NO_INTEREST_EXPENSE`(위험 아님), 음수 영업이익은 음수 배율로 계산 |
| 영업현금흐름 비율 | 영업활동현금흐름 / 유동부채 × 100 | OPERATING_CASH_FLOW, CURRENT_LIABILITIES | % | 분모 0 → `DIV_BY_ZERO` |

모든 결과는 `MetricResult{code, year, value | None, unit, status, formula, inputs{계정:값}}` 형태로 반환·저장한다. `status ≠ OK`이면 `value=None`이며 보고서에 수치로 쓰지 않는다.

### 위험 규칙 (초기 기본값, `risk_rules.yaml`, version 명시)

| 규칙 | 기본 조건 | 심각도 |
|---|---|---|
| 완전자본잠식 | 자본총계 ≤ 0 | HIGH |
| 부분자본잠식 | 0 < 자본총계 < 자본금 | MEDIUM |
| 영업손실 지속 | 최근 N(=2)개년 연속 영업이익 < 0 | HIGH |
| 이자보상배율 악화 | 최근년도 < 1.0 또는 전년 대비 하락폭 ≥ 설정값 | HIGH/MEDIUM |
| 유동성 악화 | 유동비율 < 100% 또는 전년 대비 20%p 이상 하락 | MEDIUM |
| 영업현금흐름 적자 | OCF < 0 (2년 연속 시 상향) | MEDIUM/HIGH |
| 자료 불일치·누락 | 정합성 검사 실패, 필수 계정 결측 | INFO/MEDIUM |

각 신호는 `{rule_id, rule_version, severity, evidence_ids, message}`. 보고서·화면에 "위험 신호는 부도확률이 아님" 문구 고정.

---

## 5. 데이터 스키마 및 검증 계획

### DB 테이블 (SQLite, `PRAGMA foreign_keys=ON`)

| 테이블 | 핵심 컬럼 | 관계/제약 |
|---|---|---|
| companies | id PK, company_code UNIQUE, name, industry, created_at | — |
| credit_cases | id PK, company_id FK, case_no UNIQUE, purpose, status, created_at | companies 1:N |
| documents | id PK, company_id FK, file_name, sha256 UNIQUE, stored_path, uploaded_at | 원본 파일 사본 보존 (raw) |
| financial_statements | id PK, company_id FK, document_id FK, fiscal_year, fiscal_end_date, scope CHECK(CFS/OFS), account_code, amount_krw INTEGER, source_row | UNIQUE(company_id, document_id, fiscal_year, scope, account_code) — 정제 결과 |
| analysis_runs | id PK, case_id FK, document_id FK, scope, input_hash, config_hashes(JSON), app_version, created_at | **UPDATE/DELETE 차단 트리거**. LLM 모델·프롬프트 버전은 run이 아니라 보고서 결과 행의 payload에 기록 *(Phase 1 변경: run은 수정 불가라 나중에 만드는 보고서 정보를 채울 수 없음)* |
| analysis_results | id PK, run_id FK, kind(ratio/risk/stress/report), key, fiscal_year, value, status, payload_json | 차단 트리거 |
| audit_logs | id PK, ts, action, entity, entity_id, detail_json | 차단 트리거, 키·개인정보 기록 금지 |

원본(documents + 파일 사본) → 정제(financial_statements) → 분석(analysis_*)을 계층 분리. 재분석은 항상 새 run을 추가한다.

### 입력 검증 단계 (`validation.py`, 결과는 행 단위 이슈 목록)

1. 스키마: 필수 컬럼 존재, 타입(Pydantic `StatementRow`), 금액 숫자 변환 실패 행 기록
2. 매핑: 미매핑 계정명 목록 (분석 제외하되 표시)
3. 단위: `원/천원/백만원` 외 값 오류, 정규화 후 원 단위 정수
4. 범위: CFS/OFS 혼합 감지 → 선택 정책 적용
5. 결산: `fiscal_end_date` 파싱, `fiscal_year` 일치 여부, 결산월 변경 경고
6. 중복: 같은 키(연도·범위·계정) 다중 행 — 값 동일이면 경고 후 1건, 다르면 오류
7. 결측: 필수 계정 누락 → 해당 비율 `MISSING_INPUT`
8. 이상치: 음수 자산·음수 비용 계정, 정합성(자산=부채+자본) 허용오차 초과

### 테스트 매핑 (명세 7절 ↔ 테스트 파일)

| 요구 테스트 | 파일 |
|---|---|
| 수기 계산 정답 비교 | test_ratios.py (손계산 고정 픽스처 1개사 3개년) |
| 분모 0 / 음수 자본·영업이익 | test_ratios.py |
| 결측·중복 | test_validation.py |
| 단위 혼합 | test_mapping.py |
| CFS/OFS 혼합, 결산연도 상이 | test_validation.py |
| 스트레스 경계값 (0%, −100%, 변동비중 0/1, 이자 0) | test_stress.py |
| AI 실패·잘못된 응답 (타임아웃, 비JSON, 스키마 위반, 근거 없는 수치, 결정 표현) | test_llm_failures.py (Mock만 사용) |
| 이력 저장·재조회, 덮어쓰기 차단 | test_db_history.py |
| 동일 입력·규칙 재실행 동일성 | test_reproducibility.py |

금융 계산 테스트는 네트워크·LLM 호출 없이 실행 (`pytest -m "not llm_live"`).

---

## 6. Phase 1 작업 목록

1. 영구 프로젝트 폴더 확정, `git init`, `.gitignore`
2. 가상환경 생성 (`python -m venv .venv`) 및 `pyproject.toml`에 위 버전 고정 후 설치
3. 디렉터리 골격 생성 (3절 구조, 빈 모듈 최소화 — 실제 쓰는 것만)
4. `config/*.yaml` 4종 작성 + `config.py` (Pydantic 검증, SHA-256 해시)
5. `db/schema.sql` + `repository.py` (생성, 기본 insert/select, 불변성 트리거)
6. `sample_data.py`: 시드 고정 가상 기업 4종 (정상 / 영업손실 지속 / 자본잠식 / 결측·단위혼합) CSV·XLSX 출력
7. 테스트: 스키마 생성, FK·UNIQUE 제약, 트리거로 UPDATE/DELETE 차단, 설정 로드 실패 케이스, 샘플 데이터 결정성(같은 시드 → 같은 해시)
8. Phase 1 보고: 변경 파일, 기능, `pytest` 결과, 미해결 문제

### 사용자 확인이 필요한 사항

- 프로젝트 영구 저장 위치 (현재 폴더는 세션 삭제 시 사라짐)
- 실제 LLM 제공자 (기본 제안: 어댑터 + Mock, 실제 API는 Phase 4에서 연결)
- 2절의 기본 결정 중 바꾸고 싶은 항목
