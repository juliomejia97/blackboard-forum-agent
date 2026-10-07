from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class CourseConfig(BaseModel):
    bb_course_id: str
    name: str
    notion_course_url: str
    min_replies: int = 2
    reply_scope: Literal["forum", "thread"] = "forum"
    min_reply_words: int = 30
    ai_declaration: str
    forum_launch_url: str | None = None


class AppConfig(BaseModel):
    base_url: str = "https://blackboard.ie.edu"
    profile_dir: Path = Path("profile")
    data_dir: Path = Path("data")
    out_dir: Path = Path("out")
    courses: list[CourseConfig] = []


PLACEHOLDER_COURSE_ID = "_00000_1"
PLACEHOLDER_NOTION_MARKER = "<course-page-id>"
MODEL_PLACEHOLDER = "<model>"


class ConfigError(Exception):
    """config.yaml still holds example placeholders."""


def validate_config(config: AppConfig) -> None:
    """Raise ConfigError when a course still has the config.example.yaml placeholders."""
    problems = []
    for course in config.courses:
        if course.bb_course_id == PLACEHOLDER_COURSE_ID:
            problems.append(f"bb_course_id {PLACEHOLDER_COURSE_ID} ({course.name})")
        if PLACEHOLDER_NOTION_MARKER in course.notion_course_url:
            problems.append(f"notion_course_url {PLACEHOLDER_NOTION_MARKER} ({course.name})")
        if course.ai_declaration.count(MODEL_PLACEHOLDER) != 1:
            problems.append(
                f"ai_declaration must contain {MODEL_PLACEHOLDER} exactly once ({course.name})"
            )
    if problems:
        raise ConfigError(
            "config.yaml has invalid or placeholder values: "
            + "; ".join(problems)
            + ". Edit config.yaml"
        )


def load_config(path: Path | str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f) or {})
