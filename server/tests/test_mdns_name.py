"""The Pi keeps its mDNS name: install.sh's Avahi step and the musehost-mdns check."""

import os
import re
import socket
import subprocess
from pathlib import Path

DEPLOY = Path(__file__).parents[1] / "deploy"
CHECK = DEPLOY / "musehost-mdns.sh"
INSTALL = Path(__file__).parents[1] / "install.sh"
HOST = f"{socket.gethostname().split('.')[0]}.local"

# Debian 13's avahi-daemon.conf [server] section, trimmed.
DEBIAN_CONF = """[server]
#host-name=foo
#domain-name=local
use-ipv4=yes
use-ipv6=yes
ratelimit-interval-usec=1000000
ratelimit-burst=1000

[wide-area]
enable-wide-area=yes
"""


def fake_tools(tmp_path: Path, avahi_name: str) -> dict:
    """busctl answers with ``avahi_name``; systemctl records its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "busctl").write_text(f"#!/bin/sh\necho 's \"{avahi_name}\"'\n")
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path}/systemctl.log\n')
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def check(tmp_path: Path, avahi_name: str, host: str = HOST):
    env = fake_tools(tmp_path, avahi_name)
    run = subprocess.run([CHECK, host], env=env, capture_output=True, text=True, check=True)
    log = tmp_path / "systemctl.log"
    return run.stdout, log.read_text() if log.exists() else ""


def test_the_right_name_needs_nothing(tmp_path):
    out, systemctl = check(tmp_path, HOST)
    assert (out, systemctl) == ("", "")


def test_a_renamed_avahi_is_restarted_once_with_one_log_line(tmp_path):
    renamed = HOST.replace(".local", "-2.local")
    out, systemctl = check(tmp_path, renamed)
    assert systemctl == "restart avahi-daemon\n"
    assert out == f"Avahi was {renamed}, not {HOST}; restarted it\n"


def test_a_name_avahi_does_not_publish_is_left_alone(tmp_path):
    assert check(tmp_path / "dns", "other.local", host="clio.example.com") == ("", "")
    assert check(tmp_path / "other", "other.local", host="not-this-pi.local") == ("", "")


def config_block() -> str:
    match = re.search(
        r"^# BEGIN avahi-config.*?^# END avahi-config$", INSTALL.read_text(), re.S | re.M
    )
    assert match, "install.sh lost its avahi-config block"
    return match.group(0)


def configure(conf: Path) -> str:
    script = f'{config_block()}\nconfigure_avahi "{conf}" && echo changed || echo same\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_avahi_step_turns_off_ipv6_once_and_keeps_the_original(tmp_path):
    conf = tmp_path / "avahi-daemon.conf"
    conf.write_text(DEBIAN_CONF)
    assert configure(conf) == "changed"
    assert configure(conf) == "same"
    text = conf.read_text()
    assert text.count("use-ipv6") == 1 and "use-ipv6=no\n" in text
    assert text.replace("use-ipv6=no", "use-ipv6=yes") == DEBIAN_CONF
    assert (tmp_path / "avahi-daemon.conf.musehost-orig").read_text() == DEBIAN_CONF


def test_the_avahi_step_adds_the_line_when_the_file_has_none(tmp_path):
    conf = tmp_path / "avahi-daemon.conf"
    conf.write_text(DEBIAN_CONF.replace("use-ipv6=yes\n", ""))
    assert configure(conf) == "changed"
    assert "[server]\nuse-ipv6=no\n" in conf.read_text()
