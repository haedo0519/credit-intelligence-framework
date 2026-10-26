import pandas as pd
import streamlit as st

from cif import ui
from cif.pipeline import statement_values

ui.setup("재무 분석")
cfg = ui.config()
conn = ui.db(cfg)
run = ui.select_run(conn)
if run is None:
    st.stop()

st.subheader("연도별 재무비율")
st.caption("값이 없으면 계산 상태를 표시합니다 (DIV_BY_ZERO: 분모 0, NOT_MEANINGFUL: 해석 불가, MISSING_INPUT: 계정 누락).")
st.dataframe(ui.ratio_frame(run), use_container_width=True)

names = sorted({m.name for m in run.ratios})
pick = st.multiselect("추세 그래프", names, default=[n for n in names if n in ("유동비율", "부채비율", "영업이익률")])
if pick:
    df = pd.DataFrame([{"연도": str(m.fiscal_year), "지표": m.name, "값": m.value}
                       for m in run.ratios if m.name in pick and m.value is not None])
    if not df.empty:
        st.line_chart(df.pivot(index="연도", columns="지표", values="값"))

with st.expander("계산식과 사용 계정"):
    latest = max(m.fiscal_year for m in run.ratios)
    st.dataframe(pd.DataFrame([{"지표": m.name, "계산식": m.formula, "잔액 기준": m.basis, "상태": m.status,
                                "사용 계정(원)": ", ".join(f"{k}={v:,}" if v is not None else f"{k}=없음"
                                                       for k, v in m.inputs.items()), "비고": m.note}
                               for m in run.ratios if m.fiscal_year == latest]),
                 hide_index=True, use_container_width=True)

st.subheader("주요 계정 추세 (단위: 백만원)")
values = statement_values(conn, run.document_id, run.scope)
accounts = ["REVENUE", "OPERATING_INCOME", "NET_INCOME", "OPERATING_CASH_FLOW", "TOTAL_ASSETS", "TOTAL_EQUITY"]
trend = pd.DataFrame({y: {a: values[y].get(a, float("nan")) / 1e6 for a in accounts} for y in sorted(values)})
trend.columns = [str(c) for c in trend.columns]
st.dataframe(trend.map(lambda x: "-" if pd.isna(x) else f"{x:,.0f}"), use_container_width=True)
st.bar_chart(trend.loc[["REVENUE", "OPERATING_INCOME", "OPERATING_CASH_FLOW"]].T)
conn.close()
