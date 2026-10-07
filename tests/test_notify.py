from bbagent.notify import build_command, notify


def test_build_command_escapes_quotes_and_backslashes():
    cmd = build_command('say "hi"', "run `bbagent login`\\ now\nok")
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[2] == (
        'display notification "run `bbagent login`\\\\ now ok" with title "say \\"hi\\""'
    )


def test_notify_runs_command_and_swallows_os_errors():
    calls = []
    notify("T", "M", runner=lambda cmd, **kw: calls.append(cmd))
    assert calls == [build_command("T", "M")]

    def missing(cmd, **kw):
        raise FileNotFoundError

    notify("T", "M", runner=missing)
