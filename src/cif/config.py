"""YAML 설정 로드·검증. 각 파일의 SHA-256을 함께 반환해 분석 실행(run)에 기록한다."""
import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
Scope = Literal["CFS", "OFS"]
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH"]


class Settings(BaseModel):
    version: str
    db_path: str
    raw_dir: str
    balance_tolerance: float = Field(ge=0, le=0.1)
    scope_priority: list[Scope] = Field(min_length=1)
    units: dict[str, int]

    @model_validator(mode="after")
    def _check(self):
        if len(set(self.scope_priority)) != len(self.scope_priority):
            raise ValueError("scope_priority에 중복 값이 있습니다")
        if not self.units or any(v <= 0 for v in self.units.values()):
            raise ValueError("units 배수는 양의 정수여야 합니다")
        return self


class AccountMap(BaseModel):
    version: str
    accounts: dict[str, list[str]]
    required: list[str]
    non_negative: list[str]

    @model_validator(mode="after")
    def _check(self):
        seen: dict[str, str] = {}
        for code, names in self.accounts.items():
            for name in names:
                key = normalize_account_name(name)
                if key in seen:
                    raise ValueError(f"계정명 '{name}'이 {seen[key]}와 {code}에 중복 매핑됨")
                seen[key] = code
        unknown = (set(self.required) | set(self.non_negative)) - set(self.accounts)
        if unknown:
            raise ValueError(f"정의되지 않은 계정코드: {sorted(unknown)}")
        return self

    def lookup(self) -> dict[str, str]:
        """정규화된 계정명 → 표준코드."""
        return {normalize_account_name(n): c for c, ns in self.accounts.items() for n in ns}


class Rule(BaseModel):
    description: str
    severity: Severity
    params: dict[str, float] = {}


class RiskRules(BaseModel):
    version: str
    disclaimer: str
    rules: dict[str, Rule]


class StressAssumptions(BaseModel):
    floating_rate_share: float = Field(ge=0, le=1)
    months_applied: int = Field(ge=0, le=12)
    borrowings_basis: str
    fixed_cost_share: float = Field(ge=0, le=1)


class Scenario(BaseModel):
    description: str
    revenue_change_pct: float = Field(ge=-100)  # 매출이 음수가 되는 시나리오 금지
    op_margin_change_pctp: float
    rate_change_bp: int


class StressConfig(BaseModel):
    version: str
    assumptions: StressAssumptions
    scenarios: dict[str, Scenario]

    @model_validator(mode="after")
    def _check(self):
        if "BASE" not in self.scenarios:
            raise ValueError("BASE 시나리오가 필요합니다")
        return self


class Config(BaseModel):
    settings: Settings
    accounts: AccountMap
    risk: RiskRules
    stress: StressConfig
    hashes: dict[str, str]


FILES = {
    "settings": ("settings.yaml", Settings),
    "accounts": ("account_map.yaml", AccountMap),
    "risk": ("risk_rules.yaml", RiskRules),
    "stress": ("stress_scenarios.yaml", StressConfig),
}


def normalize_account_name(name: str) -> str:
    return "".join(str(name).split())


def load_config(config_dir: Path | str = CONFIG_DIR) -> Config:
    config_dir = Path(config_dir)
    parts, hashes = {}, {}
    for key, (fname, model) in FILES.items():
        raw = (config_dir / fname).read_bytes()
        hashes[key] = hashlib.sha256(raw).hexdigest()
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError(f"{fname}: YAML 최상위가 매핑이 아닙니다")
        parts[key] = model.model_validate(data)
    return Config(**parts, hashes=hashes)
