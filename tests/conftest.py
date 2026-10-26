import pytest

from cif.analysis.ratios import compute_ratios
from cif.analysis.risk_rules import evaluate_risks
from cif.analysis.stress import run_stress
from cif.config import load_config
from cif.db import repository as repo
from cif.ingest.validation import validate_file
from cif.sample_data import COMPANIES, write_samples


@pytest.fixture
def conn(tmp_path):
    c = repo.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    write_samples(d)
    return d


@pytest.fixture(scope="session")
def report_inputs(sample_dir):
    """회사코드 → generate_report() 키워드 인자 (AI 어댑터 제외)."""
    cfg = load_config()

    def build(code: str) -> dict:
        path = sample_dir / f"{code}.csv"
        v = validate_file(path, cfg)
        values = v.values()
        ratios = compute_ratios(values)
        return dict(company_code=code, company_name=COMPANIES[code]["name"], industry=COMPANIES[code]["industry"],
                    case_no=f"CASE-{code}", validation=v, ratios=ratios,
                    risks=evaluate_risks(values, ratios, v.issues, cfg.risk),
                    stress=run_stress(values, cfg.stress), cfg=cfg, input_hash=repo.file_sha256(path))
    return build
