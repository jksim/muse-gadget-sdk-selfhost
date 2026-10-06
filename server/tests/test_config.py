from musehost.config import HostConfig


def test_the_provisioned_address_is_the_first_hostname():
    config = HostConfig(
        hostnames=("musehost.local", "musehost.lan"), ips=("192.168.1.10",), port=8443
    )
    assert config.public_host == "musehost.local:8443"
    assert config.api_url == "https://musehost.local:8443"


def test_without_a_hostname_the_provisioned_address_is_the_first_ip():
    config = HostConfig(hostnames=(), ips=("192.168.1.10", "10.0.0.2"), port=9443)
    assert config.public_host == "192.168.1.10:9443"


def test_an_ipv6_address_is_bracketed():
    assert HostConfig(hostnames=(), ips=("fd00::1",), port=8443).public_host == "[fd00::1]:8443"


def test_config_round_trips_through_host_toml(tmp_path):
    config = HostConfig(
        hostnames=("musehost.local",), ips=("192.168.1.10",), port=9443, vm_id="den"
    )
    config.save(tmp_path / "host.toml")
    assert HostConfig.load(tmp_path / "host.toml") == config


def test_on_port_443_the_provisioned_address_has_no_port():
    # The ESP32 dials 443 and uses noise_host verbatim as a hostname.
    config = HostConfig(hostnames=("muse-host.local",), ips=("192.168.4.218",), port=443)
    assert config.public_host == "muse-host.local"
    assert config.api_url == "https://muse-host.local"


def test_on_port_443_an_ip_only_host_has_no_port():
    assert HostConfig(hostnames=(), ips=("192.168.4.218",), port=443).public_host == (
        "192.168.4.218"
    )


def test_speech_settings_default_to_base_en_and_round_trip(tmp_path):
    config = HostConfig(hostnames=("muse-host.local",), ips=(), port=443)
    assert (config.speech_model, config.speech_timeout_s) == ("base.en", 30.0)
    import dataclasses

    off = dataclasses.replace(config, speech_model="", speech_timeout_s=12.5)
    off.save(tmp_path / "host.toml")
    assert HostConfig.load(tmp_path / "host.toml") == off
