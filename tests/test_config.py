import shutil

import pytest
import yaml
from pydantic import ValidationError

from cif.config import CONFIG_DIR, load_config


def test_load_default_config():
    cfg = load_config()
    assert set(cfg.hashes) == {"settings", "accounts", "risk", "stress"}
    assert all(len(h) == 64 for h in cfg.hashes.values())
    assert cfg.accounts.lookup()["영업이익(손실)"] == "OPERATING_INCOME"
    assert cfg.settings.units["백만원"] == 1_000_000
    assert "BASE" in cfg.stress.scenarios


def test_hash_is_stable():
    assert load_config().hashes == load_config().hashes


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    return d


def _edit(path, fn):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_hash_changes_when_rules_change(cfg_dir):
    before = load_config(cfg_dir).hashes["risk"]
    _edit(cfg_dir / "risk_rules.yaml", lambda d: d["rules"]["INTEREST_COVERAGE_WEAK"]["params"].update(min_ratio=1.5))
    assert load_config(cfg_dir).hashes["risk"] != before


@pytest.mark.parametrize("fname,mutate", [
    ("account_map.yaml", lambda d: d["accounts"]["REVENUE"].append("영업이익")),       # 동의어 중복
    ("account_map.yaml", lambda d: d["required"].append("UNKNOWN_CODE")),             # 미정의 코드
    ("stress_scenarios.yaml", lambda d: d["assumptions"].update(floating_rate_share=1.5)),
    ("stress_scenarios.yaml", lambda d: d["assumptions"].update(fixed_cost_share=-0.1)),
    ("stress_scenarios.yaml", lambda d: d["assumptions"].pop("fixed_cost_share")),
    ("stress_scenarios.yaml", lambda d: d["scenarios"].pop("BASE")),
    ("stress_scenarios.yaml", lambda d: d["scenarios"]["BASE"].update(revenue_change_pct=-150)),
    ("settings.yaml", lambda d: d.update(scope_priority=["CFS", "CFS"])),
    ("settings.yaml", lambda d: d.update(scope_priority=["XYZ"])),
    ("settings.yaml", lambda d: d["units"].update(원=0)),
    ("settings.yaml", lambda d: d.update(equity_rollforward_tolerance=-0.01)),
    ("risk_rules.yaml", lambda d: d["rules"]["DATA_INCONSISTENCY"].update(severity="CRITICAL")),
    ("risk_rules.yaml", lambda d: d["diagnosis"].update(peer_common_share=0)),
    ("risk_rules.yaml", lambda d: d.pop("diagnosis")),
])
def test_invalid_config_rejected(cfg_dir, fname, mutate):
    _edit(cfg_dir / fname, mutate)
    with pytest.raises(ValidationError):
        load_config(cfg_dir)


def test_non_mapping_yaml_rejected(cfg_dir):
    (cfg_dir / "settings.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="매핑"):
        load_config(cfg_dir)
