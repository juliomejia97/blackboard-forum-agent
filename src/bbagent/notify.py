import subprocess
from collections.abc import Callable


def _escape(text: str) -> str:
    """Escape for an AppleScript double-quoted string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def build_command(title: str, message: str) -> list[str]:
    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    return ["osascript", "-e", script]


def notify(title: str, message: str, runner: Callable[..., object] = subprocess.run) -> None:
    """macOS notification; a failure to notify never breaks a run."""
    try:
        runner(build_command(title, message), check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
