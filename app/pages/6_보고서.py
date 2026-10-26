import streamlit as st
import streamlit.components.v1 as components

from cif import ui
from cif.report.render import RenderError, render_html, render_pdf

ui.setup("보고서")
cfg = ui.config()
conn = ui.db(cfg)
run = ui.select_run(conn)
if run is None or run.report is None:
    if run is not None:
        st.warning("이 분석에는 보고서가 없습니다.")
    st.stop()

r = run.report
p = r.provenance
c1, c2, c3 = st.columns(3)
c1.metric("해석 방식", "AI" if p.ai_mode == "LLM" else "규칙 기반")
c2.metric("검증된 문장", p.claim_counts.get("VERIFIED", 0))
c3.metric("검증 안 된 문장", sum(v for k, v in p.claim_counts.items() if k != "VERIFIED"))
if p.ai_error:
    st.warning(f"AI 해석 실패로 규칙 기반 문장을 사용했습니다: {p.ai_error}")

html = render_html(r)
name = f"CIF_{r.overview.company_code}_{r.overview.case_no}_run{run.id}"
d1, d2 = st.columns(2)
d1.download_button("HTML 내려받기", html.encode("utf-8"), f"{name}.html", "text/html")
try:
    d2.download_button("PDF 내려받기", render_pdf(r), f"{name}.pdf", "application/pdf")
except RenderError as e:
    d2.warning(str(e))

components.html(html, height=900, scrolling=True)
conn.close()
