from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_native_bridge_override_replaces_flatpak_dependencies() -> None:
    override = (ROOT / "examples/systemd/proton-workflow-connector-native-bridge.conf").read_text()

    assert "After=\n" in override
    assert "Wants=\n" in override
    assert "After=network-online.target protonmail-bridge.service" in override
    assert "Wants=network-online.target protonmail-bridge.service" in override
    assert "protonmail-bridge-headless.service" not in override
