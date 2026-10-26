"""보고서 내보내기: 심사 건별 최신 분석을 HTML·PDF로 저장한다.

    python -m cif.export --out "<폴더>" [--import-ai outputs/live_report_DEMO02.json]

--import-ai: 이전에 받은 AI 응답(보고서 JSON)의 문장을 그대로 다시 써서 새 분석 run을 만든다.
API를 다시 호출하지 않으며, 문장은 현재 검증기로 다시 검증하고 모델명에 '저장된 응답 재사용'을 표시한다.
"""
import argparse
import datetime as dt
import re
from pathlib import Path

from cif.config import load_config
from cif.pipeline import load_run, open_db, run_analysis
from cif.report.llm import MockAdapter
from cif.report.render import RenderError, render_html, render_pdf
from cif.report.schema import AIDraft, CreditReport


def draft_from_report(report: CreditReport) -> str:
    """보고서에 저장된 AI 문장을 AIDraft JSON으로 되돌린다 (검증 상태는 버리고 문장·근거만)."""
    def claims(items):
        return [{"text": c.text, "evidence_ids": c.evidence_ids} for c in items]
    return AIDraft.model_validate({
        "trend_commentary": claims(report.trend_section.commentary),
        "ratio_commentary": claims(report.ratio_section.commentary),
        "positives": claims(report.positives), "risk_commentary": claims(report.risk_section.commentary),
        "stress_commentary": claims(report.stress_section.commentary),
        "additional_documents": [{"item": d.item, "reason": d.reason, "evidence_ids": d.evidence_ids}
                                 for d in report.additional_documents if d.source == "AI"],
        "overall_opinion": claims(report.overall_opinion), "limitations": claims(report.ai_limitations),
    }).model_dump_json()


def import_ai_report(conn, cfg, path: Path) -> int:
    report = CreditReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if report.provenance.ai_mode != "LLM":
        raise ValueError("AI 응답이 들어 있는 보고서가 아닙니다")
    row = conn.execute("SELECT r.case_id, r.document_id FROM analysis_runs r JOIN credit_cases k ON k.id = r.case_id "
                       "JOIN companies c ON c.id = k.company_id WHERE c.company_code = ? AND r.input_hash = ? "
                       "ORDER BY r.id DESC LIMIT 1",
                       (report.overview.company_code, report.provenance.input_hash)).fetchone()
    if row is None:
        raise ValueError("같은 기업·같은 입력 파일로 분석한 심사 건이 DB에 없습니다")
    adapter = MockAdapter(draft_from_report(report), model_name=f"{report.provenance.ai_model} (저장된 응답 재사용)")
    return run_analysis(conn, cfg, row["case_id"], row["document_id"], adapter)


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")


def export_latest(conn, out_dir: Path, issued: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    latest = conn.execute("SELECT MAX(id) AS id FROM analysis_runs GROUP BY case_id ORDER BY MAX(id)").fetchall()
    for (run_id,) in latest:
        run = load_run(conn, run_id)
        if run.report is None:
            continue
        r = run.report
        ai = "AI" if r.provenance.ai_mode == "LLM" else "규칙기반"
        stem = _safe(f"{r.overview.company_name}_{r.overview.case_no}_{ai}_run{run.id}")
        html = out_dir / f"{stem}.html"
        html.write_text(render_html(r, issued=issued), encoding="utf-8")
        written.append(html)
        try:
            pdf = out_dir / f"{stem}.pdf"
            pdf.write_bytes(render_pdf(r, issued=issued))
            written.append(pdf)
        except RenderError as e:
            print(f"PDF 생략 ({stem}): {e}")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="CIF 보고서 내보내기")
    ap.add_argument("--out", required=True, type=Path, help="저장할 폴더")
    ap.add_argument("--import-ai", type=Path, action="append", default=[], help="재사용할 AI 보고서 JSON")
    args = ap.parse_args(argv)
    cfg = load_config()
    conn = open_db(cfg)
    try:
        for p in args.import_ai:
            print(f"AI 응답 재사용 → 분석 #{import_ai_report(conn, cfg, p)}")
        for p in export_latest(conn, args.out, dt.date.today().isoformat()):
            print(p)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
