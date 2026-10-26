import pandas as pd
import streamlit as st

from cif import ui
from cif.ingest.loader import read_table
from cif.ingest.validation import validate_file

ui.setup("데이터 검증")
cfg = ui.config()
conn = ui.db(cfg)

docs = pd.read_sql_query("SELECT d.id, d.file_name, d.stored_path, d.uploaded_at, c.name FROM documents d "
                         "JOIN companies c ON c.id = d.company_id ORDER BY d.id DESC", conn)
if docs.empty:
    st.info("업로드된 자료가 없습니다.")
    st.stop()
label = {r.id: f"#{r.id} {r.name} · {r.file_name} ({r.uploaded_at[:19]})" for r in docs.itertuples()}
ids = list(label)
default = st.session_state.get("document_id")
doc_id = st.selectbox("자료", ids, index=ids.index(default) if default in ids else 0, format_func=label.get)
st.session_state["document_id"] = doc_id
path = docs.set_index("id").loc[doc_id, "stored_path"]

v = validate_file(path, cfg)
issues = pd.DataFrame([i.model_dump() for i in v.issues])
c1, c2, c3, c4 = st.columns(4)
c1.metric("검증 결과", "통과" if v.ok else "오류")
c2.metric("오류", int((issues["level"] == "ERROR").sum()) if not issues.empty else 0)
c3.metric("경고", int((issues["level"] == "WARNING").sum()) if not issues.empty else 0)
c4.metric("사용 범위", v.scope or "-")

tab1, tab2, tab3, tab4 = st.tabs(["오류·경고", "단위·계정과목", "정제 결과", "원본"])
with tab1:
    if issues.empty:
        st.success("발견된 문제가 없습니다.")
    else:
        st.dataframe(issues.rename(columns={"level": "수준", "code": "코드", "message": "내용", "source_row": "원본 행",
                                            "fiscal_year": "연도", "account_code": "계정"}),
                     hide_index=True, use_container_width=True)
with tab2:
    rows = pd.DataFrame([r.model_dump() for r in v.rows])
    if not rows.empty:
        st.write("단위별 행 수 (모두 원 단위로 환산됨)")
        st.dataframe(rows.groupby("raw_unit").size().rename("행 수"), use_container_width=True)
        st.write("계정과목 매핑")
        st.dataframe(rows[["raw_account_name", "account_code"]].drop_duplicates()
                     .rename(columns={"raw_account_name": "원본 계정명", "account_code": "표준 계정"}),
                     hide_index=True, use_container_width=True)
    unmapped = [i.message for i in v.issues if i.code == "UNMAPPED_ACCOUNT"]
    if unmapped:
        st.warning("매핑되지 않은 계정 (분석 제외)")
        st.write(unmapped)
with tab3:
    if v.rows:
        pivot = rows.pivot_table(index="account_code", columns="fiscal_year", values="amount_krw", aggfunc="first")
        st.caption("단위: 원")
        st.dataframe(pivot.map(lambda x: "-" if pd.isna(x) else f"{x:,.0f}"), use_container_width=True)
with tab4:
    try:
        st.dataframe(read_table(path), use_container_width=True)
    except Exception as e:  # 손상 파일도 화면은 열려야 한다
        st.error(f"원본을 표시할 수 없습니다: {e}")
conn.close()
