import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from cif import ui
from cif.db import repository as repo
from cif.pipeline import run_analysis, upload_statements

ui.setup("기업 관리")
cfg = ui.config()
conn = ui.db(cfg)
comp = ui.companies(conn)
label = {r.id: f"{r.name} ({r.company_code})" for r in comp.itertuples()}

left, right = st.columns(2)
with left.form("company", clear_on_submit=True):
    st.subheader("기업 등록")
    code = st.text_input("기업코드 (파일의 company_code와 같아야 함)")
    name = st.text_input("기업명")
    industry = st.text_input("업종")
    if st.form_submit_button("등록"):
        if not code.strip() or not name.strip():
            st.error("기업코드와 기업명은 필수입니다.")
        elif code.strip() in set(comp["company_code"]):
            st.error("이미 등록된 기업코드입니다.")
        else:
            repo.create_company(conn, code.strip(), name.strip(), industry.strip() or None)
            st.rerun()

with right.form("case", clear_on_submit=True):
    st.subheader("심사 건 등록")
    cid = st.selectbox("기업", list(label), format_func=label.get, key="case_company")
    case_no = st.text_input("심사 건 번호")
    purpose = st.text_input("목적 (예: 운전자금)")
    if st.form_submit_button("등록"):
        exists = conn.execute("SELECT 1 FROM credit_cases WHERE case_no=?", (case_no.strip(),)).fetchone()
        if cid is None or not case_no.strip():
            st.error("기업과 심사 건 번호는 필수입니다.")
        elif exists:
            st.error("이미 있는 심사 건 번호입니다.")
        else:
            repo.create_case(conn, cid, case_no.strip(), purpose.strip() or None)
            st.rerun()

st.subheader("등록 기업")
st.dataframe(comp.rename(columns={"company_code": "기업코드", "name": "기업명", "industry": "업종",
                                  "created_at": "등록 시각"}), hide_index=True, use_container_width=True)

st.divider()
st.subheader("재무자료 업로드")
st.caption("형식: company_code, fiscal_year, fiscal_end_date, statement_scope(CFS/OFS), account_name, amount, unit(원/천원/백만원)")
up_cid = st.selectbox("기업", list(label), format_func=label.get, key="upload_company")
file = st.file_uploader("CSV 또는 XLSX", type=["csv", "xlsx"])
if file is not None and up_cid is not None and st.button("업로드 및 검증"):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / Path(file.name).name  # 업로드 이름의 경로 부분은 버린다
        path.write_bytes(file.getvalue())
        res = upload_statements(conn, cfg, up_cid, path)
    v = res.validation
    errors = [i for i in v.issues if i.level == "ERROR"]
    if v.ok:
        st.success(f"검증 통과: {len(v.rows)}행 정제, 경고 {sum(i.level == 'WARNING' for i in v.issues)}건 "
                   f"(문서 #{res.document_id}). 아래에서 분석을 실행하세요.")
    else:
        st.error(f"검증 오류 {len(errors)}건: 분석할 수 없습니다. '데이터 검증' 화면에서 확인하세요.")
    if v.issues:
        st.dataframe(pd.DataFrame([i.model_dump() for i in v.issues]), hide_index=True, use_container_width=True)
    if res.document_id:
        st.session_state["document_id"] = res.document_id

st.divider()
st.subheader("분석 실행")
cases = pd.read_sql_query("SELECT k.id, k.case_no, c.name, k.company_id FROM credit_cases k "
                          "JOIN companies c ON c.id = k.company_id ORDER BY k.id DESC", conn)
if cases.empty:
    st.info("먼저 심사 건을 등록하세요.")
else:
    case_label = {r.id: f"{r.case_no} · {r.name}" for r in cases.itertuples()}
    case_id = st.selectbox("심사 건", list(case_label), format_func=case_label.get)
    company_id = int(cases.set_index("id").loc[case_id, "company_id"])
    docs = pd.read_sql_query("SELECT id, file_name, uploaded_at FROM documents WHERE company_id=? ORDER BY id DESC",
                             conn, params=(company_id,))
    if docs.empty:
        st.info("이 기업의 업로드 자료가 없습니다.")
    else:
        doc_label = {r.id: f"#{r.id} {r.file_name} ({r.uploaded_at[:19]})" for r in docs.itertuples()}
        doc_id = st.selectbox("재무자료", list(doc_label), format_func=doc_label.get)
        adapter = ui.ai_adapter_option("run_ai")
        if st.button("분석 실행", type="primary"):
            try:
                with st.spinner("분석 중..." + (" (AI 해석은 1~2분 걸릴 수 있습니다)" if adapter else "")):
                    run_id = run_analysis(conn, cfg, case_id, doc_id, adapter)
                st.session_state["run_id"] = run_id
                st.success(f"분석 #{run_id}을 저장했습니다. 다른 화면에서 결과를 확인하세요.")
            except ValueError as e:
                st.error(str(e))
conn.close()
