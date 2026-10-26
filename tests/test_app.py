"""Streamlit 화면 실행 확인 (AppTest: 브라우저 없이 스크립트를 실제로 실행)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from cif import ui
from cif.pipeline import seed_demo

APP = Path(__file__).resolve().parents[1] / "app"
PAGES = sorted((APP / "pages").glob("*.py"))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CIF_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def seeded(data_dir, sample_dir):
    cfg = ui.config()
    conn = ui.db(cfg)
    seed_demo(conn, cfg, sample_dir)
    conn.close()
    return data_dir


def _run(path: Path) -> AppTest:
    at = AppTest.from_file(str(path), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_home_empty_and_seed_button(data_dir):
    # 시연 데이터는 저장소의 data/samples를 읽기만 한다 (이미 있으면 다시 쓰지 않음). DB·원본 사본은 임시 폴더
    at = _run(APP / "Home.py")
    assert at.metric[0].value == "0"
    at.button[0].click().run()
    assert not at.exception
    assert at.metric[0].value == "4"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_pages_render_with_data(seeded, page):
    at = _run(page)
    assert at.title[0].value


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_pages_render_empty(data_dir, page):
    _run(page)  # 데이터가 없어도 오류 없이 안내만 표시


def test_register_company(data_dir):
    at = _run(APP / "pages" / "1_기업_관리.py")
    at.text_input[0].set_value("NEW01")
    at.text_input[1].set_value("새기업")
    at.button[0].click().run()
    assert not at.exception
    conn = ui.db(ui.config())
    assert conn.execute("SELECT name FROM companies WHERE company_code='NEW01'").fetchone()[0] == "새기업"
    conn.close()


def test_history_rerun_and_compare(seeded):
    at = _run(APP / "pages" / "7_분석_이력.py")
    at.button[0].click().run()  # 재분석
    assert not at.exception
    assert any("완전히 같습니다" in s.value for s in at.success)


def test_risk_page_shows_signals(seeded):
    at = _run(APP / "pages" / "4_위험_분석.py")
    assert any(m.label == "위험 신호" for m in at.metric)
