import pandas as pd
import streamlit as st

from cif import ui

ui.setup("위험 분석")
cfg = ui.config()
conn = ui.db(cfg)
run = ui.select_run(conn)
if run is None:
    st.stop()

st.warning(cfg.risk.disclaimer)
groups = {"TRIGGERED": [], "NOT_EVALUATED": [], "NOT_TRIGGERED": []}
for e in run.risks:
    groups[e.status].append(e)

c1, c2, c3 = st.columns(3)
c1.metric("위험 신호", len(groups["TRIGGERED"]))
c2.metric("판단 불가 (자료 부족)", len(groups["NOT_EVALUATED"]))
c3.metric("해당 없음", len(groups["NOT_TRIGGERED"]))

for status, title in (("TRIGGERED", "탐지된 위험 신호"), ("NOT_EVALUATED", "판단하지 못한 규칙"),
                      ("NOT_TRIGGERED", "해당 없음")):
    st.subheader(title)
    if not groups[status]:
        st.write("없음")
        continue
    st.dataframe(pd.DataFrame([{
        "심각도": f"{ui.SEVERITY_ICON.get(e.severity, '')} {e.severity}" if e.severity else "-",
        "규칙": e.description, "기준 연도": e.fiscal_year, "근거": e.message,
        "근거 ID": ", ".join(e.evidence_ids), "규칙 버전": e.rule_version,
    } for e in groups[status]]), hide_index=True, use_container_width=True)

with st.expander("적용한 규칙 기준값 (config/risk_rules.yaml)"):
    st.json({rid: {"설명": r.description, "심각도": r.severity, "기준": r.params} for rid, r in cfg.risk.rules.items()})
conn.close()
