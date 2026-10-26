import pandas as pd
import streamlit as st

from cif import ui
from cif.analysis.stress import run_stress
from cif.config import Scenario
from cif.pipeline import statement_values

ui.setup("시나리오 분석")
cfg = ui.config()
conn = ui.db(cfg)
run = ui.select_run(conn)
if run is None:
    st.stop()

st.caption("여기서 바꾼 값은 저장되지 않는 탐색용 계산입니다. 저장된 분석은 설정 파일의 기본 가정을 사용합니다.")
a = cfg.stress.assumptions
left, right = st.columns(2)
with left:
    st.markdown("**사용자 시나리오**")
    rev = st.slider("매출 변화 (%)", -100.0, 50.0, -10.0, 1.0)
    margin = st.slider("영업이익률 변화 (%p)", -20.0, 10.0, -2.0, 0.5)
    bp = st.slider("금리 변화 (bp)", -300, 500, 100, 25)
with right:
    st.markdown("**가정**")
    share = st.slider("변동금리 차입 비중", 0.0, 1.0, a.floating_rate_share, 0.05)
    months = st.slider("금리 적용 개월", 0, 12, a.months_applied)
    fixed = st.slider("고정비 비중 (영업비용 중)", 0.0, 1.0, a.fixed_cost_share, 0.05,
                      help="0이면 비용 전부를 변동비로 봅니다. 영업손실 기업은 매출 감소 시 손실이 줄어드는 것처럼 계산됩니다.")

assumptions = a.model_copy(update={"floating_rate_share": share, "months_applied": months, "fixed_cost_share": fixed})
scenarios = {**cfg.stress.scenarios,
             "CUSTOM": Scenario(description="사용자 시나리오", revenue_change_pct=rev, op_margin_change_pctp=margin,
                                rate_change_bp=bp)}
values = statement_values(conn, run.document_id, run.scope)
results = run_stress(values, cfg.stress, scenarios=scenarios, assumptions=assumptions)

df = pd.DataFrame([{
    "시나리오": r.description, "상태": r.status,
    "매출액": ui.won_m(r.revenue), "영업이익": ui.won_m(r.operating_income),
    "이자비용": ui.won_m(r.interest_expense),
    "이자보상배율": "-" if r.interest_coverage is None else f"{r.interest_coverage:.2f}",
    "영업이익 변화": ui.won_m((r.delta_vs_base or {}).get("operating_income")),
    "세전이익 변화": ui.won_m(r.pretax_income_change),
    "비고": " ".join(r.notes),
} for r in results])
st.subheader(f"{results[0].fiscal_year}년 기준 결과 (단위: 백만원)")
st.dataframe(df, hide_index=True, use_container_width=True)

chart = pd.DataFrame({"영업이익(백만원)": {r.description: r.operating_income / 1e6
                                       for r in results if r.operating_income is not None}})
st.bar_chart(chart)
st.caption("적용 순서: " + " → ".join(results[0].steps) + ". 법인세 효과는 반영하지 않습니다.")
conn.close()
