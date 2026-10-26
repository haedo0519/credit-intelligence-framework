"""업로드 → 검증 → 정제 저장 → 분석 → 보고서 → 이력 저장을 한 번에 묶는다. 화면(app/)은 이 함수들만 호출한다."""
import json
from pathlib import Path

from pydantic import BaseModel

from cif import __version__
from cif.analysis.diagnosis import Peer
from cif.analysis.ratios import MetricResult, compute_ratios
from cif.analysis.risk_rules import RuleEvaluation, evaluate_risks
from cif.analysis.stress import ScenarioResult, run_stress
from cif.config import Config
from cif.db import repository as repo
from cif.ingest.mapping import Issue
from cif.ingest.validation import ValidationResult, validate_file
from cif.report.generator import generate_report
from cif.report.llm import LLMAdapter
from cif.report.schema import CreditReport

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def open_db(cfg: Config):
    return repo.connect(project_path(cfg.settings.db_path))


class UploadResult(BaseModel):
    document_id: int | None
    validation: ValidationResult
    stored_rows: int = 0


def upload_statements(conn, cfg: Config, company_id: int, path: Path | str) -> UploadResult:
    """원본을 보존하고 검증한다. 검증을 통과하면 정제 결과를 저장한다 (같은 문서는 한 번만)."""
    v = validate_file(path, cfg)
    code = conn.execute("SELECT company_code FROM companies WHERE id=?", (company_id,)).fetchone()["company_code"]
    if v.company_code is not None and v.company_code != code:
        v.issues.append(Issue(level="ERROR", code="COMPANY_MISMATCH",
                              message=f"파일의 기업코드({v.company_code})가 선택한 기업({code})과 다릅니다"))
        v.ok = False
    # 읽을 수 없거나 다른 기업 자료면 이 기업의 원본으로 보관하지 않는다
    if any(i.code in ("UNREADABLE_FILE", "COMPANY_MISMATCH") for i in v.issues):
        return UploadResult(document_id=None, validation=v)
    doc_id = repo.store_document(conn, company_id, path, project_path(cfg.settings.raw_dir))
    stored = 0
    if v.ok and not repo.get_statements(conn, doc_id, v.scope):
        stored = repo.insert_statements(conn, [
            {**r.model_dump(exclude={"company_code"}), "fiscal_end_date": str(r.fiscal_end_date),
             "company_id": company_id, "document_id": doc_id} for r in v.rows])
    with conn:
        repo.log(conn, "validate", "documents", doc_id,
                 {"ok": v.ok, "errors": sum(i.level == "ERROR" for i in v.issues), "rows": stored})
    return UploadResult(document_id=doc_id, validation=v, stored_rows=stored)


def find_peers(conn, cfg: Config, company_id: int, industry: str | None, scope: str) -> list[Peer]:
    """같은 업종으로 등록된 다른 기업의 최신 자료 중 검증을 통과하고 재무제표 범위가 같은 것."""
    if not industry:
        return []
    peers = []
    rows = conn.execute("SELECT c.id, c.company_code, c.name, d.stored_path, d.sha256 FROM companies c "
                        "JOIN documents d ON d.id = (SELECT MAX(id) FROM documents WHERE company_id = c.id) "
                        "WHERE c.industry = ? AND c.id != ? ORDER BY c.company_code", (industry, company_id))
    for r in rows:
        v = validate_file(r["stored_path"], cfg)
        if v.ok and v.scope == scope:
            peers.append(Peer(code=r["company_code"], name=r["name"], values=v.values(), input_hash=r["sha256"]))
    return peers


def run_analysis(conn, cfg: Config, case_id: int, document_id: int, adapter: LLMAdapter | None = None) -> int:
    """문서의 원본 파일을 다시 검증해 분석하고 새 run으로 저장한다. 이전 run은 그대로 남는다."""
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    row = conn.execute("SELECT c.*, k.case_no FROM credit_cases k JOIN companies c ON c.id = k.company_id "
                       "WHERE k.id=?", (case_id,)).fetchone()
    if doc is None or row is None:
        raise ValueError("문서 또는 심사 건을 찾을 수 없습니다")
    if doc["company_id"] != row["id"]:
        raise ValueError("문서와 심사 건의 기업이 다릅니다")
    # 보존된 원본 사본으로 재검증: 같은 원본·같은 설정이면 같은 결과
    v = validate_file(doc["stored_path"], cfg)
    if not v.ok:
        raise ValueError("검증 오류가 있는 문서는 분석할 수 없습니다")
    values = v.values()
    ratios = compute_ratios(values)
    risks = evaluate_risks(values, ratios, v.issues, cfg.risk)
    stress = run_stress(values, cfg.stress)
    report = generate_report(company_code=row["company_code"], company_name=row["name"], industry=row["industry"],
                             case_no=row["case_no"], validation=v, ratios=ratios, risks=risks, stress=stress,
                             cfg=cfg, input_hash=doc["sha256"], adapter=adapter,
                             peers=find_peers(conn, cfg, row["id"], row["industry"], v.scope))
    results = (
        [{"kind": "validation", "key": i.code, "fiscal_year": i.fiscal_year, "status": i.level,
          "payload": i.model_dump()} for i in v.issues]
        + [{"kind": "ratio", "key": m.code, "fiscal_year": m.fiscal_year, "value": m.value, "status": m.status,
            "payload": m.model_dump()} for m in ratios]
        + [{"kind": "risk", "key": e.rule_id, "fiscal_year": e.fiscal_year, "status": e.status,
            "payload": e.model_dump()} for e in risks]
        + [{"kind": "stress", "key": s.scenario_id, "fiscal_year": s.fiscal_year, "status": s.status,
            "value": s.interest_coverage, "payload": s.model_dump()} for s in stress]
        + [{"kind": "report", "key": "CREDIT_REPORT", "status": report.provenance.ai_mode,
            "payload": report.model_dump(mode="json")}]
    )
    return repo.create_run(conn, case_id, document_id, v.scope, doc["sha256"], cfg.hashes, __version__, results)


def seed_demo(conn, cfg: Config, samples_dir: Path = PROJECT_ROOT / "data" / "samples") -> list[int]:
    """시연용: 가상 기업 4곳을 등록하고 자료를 올려 규칙 기반 분석을 1회씩 실행한다. 이미 있으면 건너뛴다."""
    from cif.sample_data import COMPANIES, write_samples

    if not all((samples_dir / f"{code}.csv").exists() for code in COMPANIES):
        write_samples(samples_dir)  # 이미 있으면 다시 쓰지 않는다 (XLSX는 쓸 때마다 바이트가 바뀜)
    paths = {p.name: p for p in samples_dir.glob("*.csv")}
    run_ids = []
    for code, info in COMPANIES.items():
        if conn.execute("SELECT 1 FROM companies WHERE company_code=?", (code,)).fetchone():
            continue
        cid = repo.create_company(conn, code, info["name"], info["industry"])
        case_id = repo.create_case(conn, cid, f"DEMO-{code}", f"시연용 ({info['case']})")
        up = upload_statements(conn, cfg, cid, paths[f"{code}.csv"])
        if up.validation.ok:
            run_ids.append(run_analysis(conn, cfg, case_id, up.document_id))
    return run_ids


class RunView(BaseModel):
    id: int
    case_id: int
    document_id: int
    scope: str
    input_hash: str
    config_hashes: dict[str, str]
    app_version: str
    created_at: str
    issues: list[Issue]
    ratios: list[MetricResult]
    risks: list[RuleEvaluation]
    stress: list[ScenarioResult]
    report: CreditReport | None


def load_run(conn, run_id: int) -> RunView | None:
    run = repo.get_run(conn, run_id)
    if run is None:
        return None
    def by(kind: str) -> list[dict]:
        return [r["payload"] for r in run["results"] if r["kind"] == kind]

    reports = by("report")
    return RunView(**{k: run[k] for k in ("id", "case_id", "document_id", "scope", "input_hash", "config_hashes",
                                          "app_version", "created_at")},
                   issues=by("validation"), ratios=by("ratio"), risks=by("risk"), stress=by("stress"),
                   report=reports[0] if reports else None)


def statement_values(conn, document_id: int, scope: str) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for r in repo.get_statements(conn, document_id, scope):
        out.setdefault(r["fiscal_year"], {})[r["account_code"]] = r["amount_krw"]
    return out


class RunDiff(BaseModel):
    config_changed: dict[str, tuple[str, str]]
    input_changed: bool
    ratio_changes: list[dict]
    risk_changes: list[dict]
    identical_results: bool


def compare_runs(a: RunView, b: RunView) -> RunDiff:
    """a(이전) → b(현재) 비교. 비율은 값·상태가 다른 항목, 위험은 상태·심각도가 다른 규칙만."""
    ra = {(m.code, m.fiscal_year): m for m in a.ratios}
    rb = {(m.code, m.fiscal_year): m for m in b.ratios}
    ratio_changes = []
    for key in sorted(set(ra) | set(rb)):
        x, y = ra.get(key), rb.get(key)
        vx, vy = (x.value if x else None), (y.value if y else None)
        sx, sy = (x.status if x else "-"), (y.status if y else "-")
        if vx != vy or sx != sy:
            ratio_changes.append({"code": key[0], "fiscal_year": key[1], "before": vx, "after": vy,
                                  "diff": (vy - vx) if vx is not None and vy is not None else None,
                                  "status_before": sx, "status_after": sy})
    def sig(e: RuleEvaluation | None) -> str:
        return f"{e.status}/{e.severity or '-'}" if e else "-"

    ea, eb = {e.rule_id: e for e in a.risks}, {e.rule_id: e for e in b.risks}
    risk_changes = [{"rule_id": k, "before": sig(ea.get(k)), "after": sig(eb.get(k))}
                    for k in sorted(set(ea) | set(eb)) if sig(ea.get(k)) != sig(eb.get(k))]
    cfg_changed = {k: (a.config_hashes.get(k, "-"), b.config_hashes.get(k, "-"))
                   for k in sorted(set(a.config_hashes) | set(b.config_hashes))
                   if a.config_hashes.get(k) != b.config_hashes.get(k)}
    def dump(r: RunView) -> str:
        return json.dumps([r.issues, r.ratios, r.risks, r.stress], default=lambda o: o.model_dump(mode="json"),
                          sort_keys=True)

    return RunDiff(config_changed=cfg_changed, input_changed=a.input_hash != b.input_hash,
                   ratio_changes=ratio_changes, risk_changes=risk_changes, identical_results=dump(a) == dump(b))
