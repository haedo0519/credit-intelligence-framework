-- 계층: 원본(documents + 파일 사본) → 정제(financial_statements) → 분석(analysis_runs/results)
-- 분석 이력과 감사 로그는 추가만 허용 (UPDATE/DELETE 트리거로 차단)

CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY,
    company_code  TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    industry      TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS credit_cases (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    case_no     TEXT NOT NULL UNIQUE,
    purpose     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    file_name    TEXT NOT NULL,
    sha256       TEXT NOT NULL CHECK (length(sha256) = 64),
    stored_path  TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (company_id, sha256)
);

CREATE TABLE IF NOT EXISTS financial_statements (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    fiscal_year       INTEGER NOT NULL CHECK (fiscal_year BETWEEN 1900 AND 2100),
    fiscal_end_date   TEXT NOT NULL,
    scope             TEXT NOT NULL CHECK (scope IN ('CFS', 'OFS')),
    account_code      TEXT NOT NULL,
    amount_krw        INTEGER NOT NULL,
    raw_account_name  TEXT NOT NULL,
    raw_amount        TEXT NOT NULL,
    raw_unit          TEXT NOT NULL,
    source_row        INTEGER NOT NULL,
    UNIQUE (company_id, document_id, fiscal_year, scope, account_code)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id             INTEGER PRIMARY KEY,
    case_id        INTEGER NOT NULL REFERENCES credit_cases(id) ON DELETE RESTRICT,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    scope          TEXT NOT NULL CHECK (scope IN ('CFS', 'OFS')),
    input_hash     TEXT NOT NULL,
    config_hashes  TEXT NOT NULL,   -- JSON: {settings, accounts, risk, stress}
    app_version    TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE RESTRICT,
    kind          TEXT NOT NULL CHECK (kind IN ('validation', 'ratio', 'risk', 'stress', 'report')),
    key           TEXT NOT NULL,
    fiscal_year   INTEGER,
    value         REAL,
    status        TEXT NOT NULL,
    payload_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_results_run ON analysis_results(run_id, kind);

CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    action       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    entity_id    INTEGER,
    detail_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TRIGGER IF NOT EXISTS no_update_runs BEFORE UPDATE ON analysis_runs
BEGIN SELECT RAISE(ABORT, 'analysis_runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_runs BEFORE DELETE ON analysis_runs
BEGIN SELECT RAISE(ABORT, 'analysis_runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_update_results BEFORE UPDATE ON analysis_results
BEGIN SELECT RAISE(ABORT, 'analysis_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_results BEFORE DELETE ON analysis_results
BEGIN SELECT RAISE(ABORT, 'analysis_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_update_audit BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_audit BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
-- 원본 문서와 정제 결과도 수정 금지 (재업로드는 새 document로 추가)
CREATE TRIGGER IF NOT EXISTS no_update_documents BEFORE UPDATE ON documents
BEGIN SELECT RAISE(ABORT, 'documents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_update_statements BEFORE UPDATE ON financial_statements
BEGIN SELECT RAISE(ABORT, 'financial_statements are immutable'); END;
