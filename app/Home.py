import json

import pandas as pd
import streamlit as st

from cif import ui
from cif.pipeline import seed_demo

ui.setup("Credit Intelligence Framework")
cfg = ui.config()
conn = ui.db(cfg)

n_comp = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
n_case = conn.execute("SELECT COUNT(*) FROM credit_cases").fetchone()[0]
n_run = conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
c1, c2, c3 = st.columns(3)
c1.metric("등록 기업", n_comp)
c2.metric("심사 건", n_case)
c3.metric("분석 실행", n_run)

if n_comp == 0:
    st.info("등록된 기업이 없습니다. 가상 기업 4곳(정상·영업손실·자본잠식·결함 데이터)으로 시연할 수 있습니다.")
    if st.button("시연 데이터 불러오기", type="primary"):
        with st.spinner("가상 데이터를 등록하고 분석하는 중..."):
            ids = seed_demo(conn, cfg)
        st.success(f"분석 {len(ids)}건을 만들었습니다.")
        st.rerun()

st.subheader("최근 분석 건")
df = ui.runs(conn)
if df.empty:
    st.write("아직 분석이 없습니다.")
else:
    view = df.head(10)[["run_id", "created_at", "company", "case_no", "ai_mode", "signals", "high"]]
    st.dataframe(view.rename(columns={"run_id": "실행", "created_at": "시각", "company": "기업", "case_no": "심사 건",
                                      "ai_mode": "해석 방식", "signals": "위험 신호", "high": "HIGH"}),
                 hide_index=True, use_container_width=True)

    st.subheader("위험 신호 요약 (심사 건별 최신 분석)")
    latest = df.sort_values("run_id").groupby("case_id").tail(1)
    rows = []
    for r in latest.itertuples():
        for (payload,) in conn.execute("SELECT payload_json FROM analysis_results WHERE run_id=? AND kind='risk' "
                                       "AND status='TRIGGERED'", (r.run_id,)):
            e = json.loads(payload)
            rows.append({"기업": r.company, "심각도": f"{ui.SEVERITY_ICON.get(e['severity'], '')} {e['severity']}",
                         "위험 신호": e["description"], "근거": e["message"]})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.write("탐지된 위험 신호가 없습니다.")
conn.close()
