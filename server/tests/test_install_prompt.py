"""install.sh's dashboard password prompt, run for real under a pseudo-terminal."""

import os
import pty
import re
import select
import subprocess
import time
from pathlib import Path

import pytest

INSTALL = Path(__file__).parents[1] / "install.sh"
GOOD = "correct horse battery"


def prompt_block() -> str:
    text = INSTALL.read_text()
    match = re.search(
        r"^# BEGIN dashboard-password.*?^# END dashboard-password$", text, re.S | re.M
    )
    assert match, "install.sh lost its dashboard-password block"
    return match.group(0)


def script(out: Path) -> str:
    # `run` stands in for the installed musehost: it records what --stdin got.
    return (
        f'run() {{ [ "$*" = "dashboard-password --stdin" ] && cat > {out}; }}\n'
        f"{prompt_block()}\n"
        'dashboard_on=0\nask_dashboard_password\necho "RESULT dashboard_on=$dashboard_on"\n'
    )


def on_a_terminal(tmp_path, answers):
    """Run the block with a controlling terminal, answering each prompt in turn."""
    out = tmp_path / "stdin-got"
    fd, child = pty.openpty()
    # A session leader that opens a terminal gets it as its controlling one,
    # so /dev/tty works in the block, as it does for someone logged in.
    tty_name = os.ttyname(child)  # kept open until the end, so reads never see a hang-up
    proc = subprocess.Popen(
        ["bash", "-c", f"exec 0<>{tty_name} 1>&0 2>&0\n{script(out)}"],
        start_new_session=True,
    )
    seen = b""
    answers = list(answers)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            seen += os.read(fd, 1024)
        elif proc.poll() is not None:
            break
        # Answer only once the prompt is up (and echo is off), like a person would.
        prompts = seen.count(b"password") + seen.count(b"Again")
        if answers and prompts > 0 and seen.rstrip().endswith((b":", b": ")):
            time.sleep(0.05)
            os.write(fd, answers.pop(0).encode() + b"\r")
            seen += b"\n"  # so the same prompt isn't answered twice
    proc.wait(timeout=5)
    os.close(fd)
    os.close(child)
    text = seen.decode(errors="replace")
    return text, out.read_text() if out.exists() else None


def test_a_chosen_password_is_set_and_never_echoed(tmp_path):
    text, got = on_a_terminal(tmp_path, [GOOD, GOOD])
    assert got == GOOD + "\n"
    assert GOOD not in text
    assert "RESULT dashboard_on=1" in text


def test_enter_skips_and_leaves_the_dashboard_off(tmp_path):
    text, got = on_a_terminal(tmp_path, [""])
    assert got is None
    assert "RESULT dashboard_on=0" in text
    assert "musehost dashboard-password" in text


@pytest.mark.parametrize(
    "answers",
    [
        ["too short", GOOD, GOOD],  # short, then fine
        [GOOD, GOOD + "x", GOOD, GOOD],  # mismatch, then fine
    ],
)
def test_a_short_or_mismatched_password_asks_again(tmp_path, answers):
    text, got = on_a_terminal(tmp_path, answers)
    assert got == GOOD + "\n"
    assert "RESULT dashboard_on=1" in text


def test_it_gives_up_after_three_tries(tmp_path):
    text, got = on_a_terminal(tmp_path, ["short1", "short2", "short3", GOOD, GOOD])
    assert got is None
    assert "RESULT dashboard_on=0" in text
    assert text.count("12 characters") >= 3


def test_with_no_terminal_it_is_skipped(tmp_path):
    out = tmp_path / "stdin-got"
    result = subprocess.run(
        ["bash", "-c", script(out)],
        input=GOOD + "\n" + GOOD + "\n",
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=15,
    )
    assert "RESULT dashboard_on=0" in result.stdout
    assert not out.exists()


def test_only_a_first_install_asks():
    text = INSTALL.read_text()
    calls = [m.start() for m in re.finditer(r"^\s*ask_dashboard_password\s*$", text, re.M)]
    assert len(calls) == 1
    before = text[: calls[0]]
    guard = before.rindex("if ")
    assert '"$first_install" = 1' in before[guard:]


def test_install_sh_is_shellcheck_clean():
    import shutil

    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck isn't installed")
    subprocess.run(["shellcheck", str(INSTALL)], check=True)
