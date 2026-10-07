from pathlib import Path

import pytest

from bbagent.cli import main
from bbagent.config import AppConfig, ConfigError, load_config, validate_config

COURSE_YAML = """
courses:
  - bb_course_id: {course_id}
    name: C
    notion_course_url: {url}
    ai_declaration: {declaration}
"""


def write(
    tmp_path: Path,
    course_id="_1_1",
    url="https://www.notion.so/x",
    declaration="'Drafted with <model>.'",
) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        COURSE_YAML.format(course_id=course_id, url=url, declaration=declaration), encoding="utf-8"
    )
    return path


def test_load_config_defaults_and_course_fields(tmp_path: Path) -> None:
    config = load_config(write(tmp_path))
    assert config.base_url == "https://blackboard.ie.edu"
    assert config.courses[0].bb_course_id == "_1_1"
    assert (config.courses[0].min_replies, config.courses[0].reply_scope) == (2, "forum")


def test_load_config_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(path) == AppConfig()


def test_validate_accepts_real_values(tmp_path: Path) -> None:
    validate_config(load_config(write(tmp_path)))


def test_validate_rejects_placeholder_course_id(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="_00000_1"):
        validate_config(load_config(write(tmp_path, course_id="_00000_1")))


def test_validate_rejects_placeholder_notion_url(tmp_path: Path) -> None:
    url = "https://www.notion.so/<course-page-id>"
    with pytest.raises(ConfigError, match="course-page-id"):
        validate_config(load_config(write(tmp_path, url=url)))


@pytest.mark.parametrize("declaration", ["declared", "'<model> and <model>'"])
def test_validate_requires_model_placeholder_exactly_once(tmp_path: Path, declaration: str) -> None:
    with pytest.raises(ConfigError, match="<model> exactly once"):
        validate_config(load_config(write(tmp_path, declaration=declaration)))


def test_validate_accepts_no_courses() -> None:
    validate_config(AppConfig(courses=[]))


def test_collect_exits_1_on_placeholder_config(tmp_path: Path, capsys, monkeypatch) -> None:
    path = write(tmp_path, course_id="_00000_1")
    monkeypatch.setattr("bbagent.cli.open_context", lambda *a, **k: pytest.fail("browser opened"))
    assert main(["collect", "--config", str(path)]) == 1
    assert "Edit config.yaml" in capsys.readouterr().err
