"""Streamlit 화면 공통 도우미. 계산은 하지 않고 pipeline·분석 모듈을 호출해 표로 보여준다."""
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from cif.config import Config, load_config
from cif.pipeline import RunView, load_run, open_db

DISCLAIMER = ("포트폴리오용 분석 도구입니다. 실제 금융기관 심사 시스템이 아니며 대출 승인·거절을 결정하지 않습니다. "
              "위험 신호는 부도확률이나 신용등급이 아닙니다.")
SEVERITY_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡", "INFO": "⚪"}


def setup(title: str):
    st.set_page_config(page_title=f"CIF · {title}", page_icon="📊", layout="wide")
    st.title(title)
    st.caption(DISCLAIMER)


def config() -> Config:
    """CIF_DATA_DIR 환경변수로 DB·원본 보관 위치를 바꿀 수 있다 (테스트·시연 분리용)."""
    cfg = load_config()
    data_dir = os.environ.get("CIF_DATA_DIR")
    if data_dir:
        cfg.settings = cfg.settings.model_copy(update={"db_path": str(Path(data_dir) / "cif.db"),
                                                       "raw_dir": str(Path(data_dir) / "raw")})
    return cfg


def db(cfg: Config):
    # Streamlit은 실행마다 스레드가 달라질 수 있어 연결을 매번 새로 연다 (SQLite는 가볍다)
    return open_db(cfg)


def companies(conn) -> pd.DataFrame:
    return pd.read_sql_query("SELECT id, company_code, name, industry, created_at FROM companies ORDER BY id", conn)


def runs(conn, case_id: int | None = None) -> pd.DataFrame:
    sql = """
        SELECT r.id AS run_id, r.created_at, c.company_code, c.name AS company, k.case_no, r.case_id,
               r.document_id, r.scope, substr(r.input_hash, 1, 12) AS input_hash, r.app_version,
               (SELECT status FROM analysis_results WHERE run_id = r.id AND kind = 'report') AS ai_mode,
               (SELECT COUNT(*) FROM analysis_results WHERE run_id = r.id AND kind = 'risk'
                  AND status = 'TRIGGERED' AND payload_json LIKE '%"severity": "HIGH"%') AS high,
               (SELECT COUNT(*) FROM analysis_results WHERE run_id = r.id AND kind = 'risk'
                  AND status = 'TRIGGERED') AS signals
        FROM analysis_runs r JOIN credit_cases k ON k.id = r.case_id JOIN companies c ON c.id = k.company_id
    """
    params = ()
    if case_id is not None:
        sql += " WHERE r.case_id = ?"
        params = (case_id,)
    df = pd.read_sql_query(sql + " ORDER BY r.id DESC", conn, params=params)
    df["ai_mode"] = df["ai_mode"].map({"LLM": "AI", "RULE_BASED_FALLBACK": "규칙 기반"}).fillna("-")
    return df


def select_run(conn, label: str = "분석 실행 선택") -> RunView | None:
    df = runs(conn)
    if df.empty:
        st.info("저장된 분석이 없습니다. '기업 관리'에서 자료를 올리고 분석을 실행하거나, 홈에서 시연 데이터를 불러오세요.")
        return None
    options = df["run_id"].tolist()
    names = {r.run_id: f"#{r.run_id} · {r.company} · {r.case_no} · {r.created_at[:19]}" for r in df.itertuples()}
    # 다른 화면에서 고른 실행을 이어서 보여준다
    default = st.session_state.get("run_id")
    index = options.index(default) if default in options else 0
    run_id = st.selectbox(label, options, index=index, format_func=names.get)
    st.session_state["run_id"] = run_id
    return load_run(conn, run_id)


def ai_adapter_option(key: str):
    """AI 사용 체크박스. 키가 없으면 비활성화하고 규칙 기반 문장을 쓴다."""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    use = st.checkbox("AI 해석 사용 (Claude API)", value=False, disabled=not has_key, key=key,
                      help=None if has_key else "ANTHROPIC_API_KEY 환경변수를 설정한 뒤 앱을 실행하면 사용할 수 있습니다.")
    if not use:
        return None
    from cif.report.llm import AnthropicAdapter, LLMError
    try:
        return AnthropicAdapter()
    except LLMError as e:
        st.warning(str(e))
        return None


def ratio_frame(run: RunView) -> pd.DataFrame:
    """지표 × 연도 표. OK가 아니면 값 대신 상태를 표시한다."""
    rows = {}
    for m in run.ratios:
        cell = f"{m.value:,.2f}" if m.value is not None else m.status
        rows.setdefault(f"{m.name} ({m.unit})", {})[m.fiscal_year] = cell
    return pd.DataFrame(rows).T


def won_m(v) -> str:
    return "-" if v is None else f"{v / 1e6:,.0f}"
