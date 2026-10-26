import pandas as pd
import streamlit as st

from cif import ui
from cif.pipeline import compare_runs, load_run, run_analysis

ui.setup("분석 이력")
cfg = ui.config()
conn = ui.db(cfg)

cases = pd.read_sql_query("SELECT k.id, k.case_no, c.name FROM credit_cases k JOIN companies c ON c.id = k.company_id "
                          "ORDER BY k.id", conn)
if cases.empty:
    st.info("심사 건이 없습니다.")
    st.stop()
label = {r.id: f"{r.case_no} · {r.name}" for r in cases.itertuples()}
case_id = st.selectbox("심사 건", list(label), format_func=label.get)
df = ui.runs(conn, case_id)
st.caption("분석 이력은 수정·삭제할 수 없으며, 재분석은 항상 새 실행으로 추가됩니다.")
st.dataframe(df[["run_id", "created_at", "document_id", "scope", "input_hash", "app_version", "ai_mode", "signals"]]
             .rename(columns={"run_id": "실행", "created_at": "시각", "document_id": "자료", "scope": "범위",
                              "input_hash": "입력 해시", "app_version": "버전", "ai_mode": "해석 방식",
                              "signals": "위험 신호"}),
             hide_index=True, use_container_width=True)

if not df.empty and st.button("같은 자료·현재 규칙으로 재분석 (규칙 기반)"):
    latest = df.iloc[0]
    new_id = run_analysis(conn, cfg, case_id, int(latest["document_id"]))
    st.success(f"새 실행 #{new_id}을 추가했습니다.")
    st.rerun()

if len(df) < 2:
    st.info("비교하려면 이 심사 건에 분석이 2건 이상 있어야 합니다.")
    st.stop()

ids = df["run_id"].tolist()
c1, c2 = st.columns(2)
before = c1.selectbox("이전 실행", ids, index=1)
after = c2.selectbox("현재 실행", ids, index=0)
diff = compare_runs(load_run(conn, before), load_run(conn, after))

if diff.identical_results:
    st.success("두 실행의 검증·비율·위험·스트레스 결과가 완전히 같습니다.")
m1, m2, m3 = st.columns(3)
m1.metric("입력 자료 변경", "예" if diff.input_changed else "아니오")
m2.metric("설정 변경", len(diff.config_changed))
m3.metric("비율 변화 항목", len(diff.ratio_changes))
if diff.config_changed:
    st.write("변경된 설정 파일 (해시 앞 12자리)")
    st.dataframe(pd.DataFrame([{"설정": k, "이전": a[:12], "현재": b[:12]} for k, (a, b) in diff.config_changed.items()]),
                 hide_index=True)
if diff.ratio_changes:
    st.subheader("재무비율 변화")
    st.dataframe(pd.DataFrame(diff.ratio_changes), hide_index=True, use_container_width=True)
if diff.risk_changes:
    st.subheader("위험 신호 변화")
    st.dataframe(pd.DataFrame(diff.risk_changes), hide_index=True, use_container_width=True)
conn.close()
