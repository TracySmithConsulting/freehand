"""The install setup (install/compose.yml) runs FreeHand from published
images, so an install needs no source code (docs/design/granny-installer.md,
step 1). It must stay in step with the developers' compose.yml."""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = yaml.safe_load((ROOT / "install" / "compose.yml").read_text(encoding="utf-8"))
DEV = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))
WORKFLOW = (ROOT / ".github" / "workflows" / "images.yml").read_text(encoding="utf-8")


def test_nothing_is_built_everything_is_an_image():
    for name, service in INSTALL["services"].items():
        assert "build" not in service, name
        assert service.get("image"), name


def test_freehand_and_the_bridge_come_from_this_projects_published_images():
    services = INSTALL["services"]
    assert services["freehand"]["image"] == "ghcr.io/tracysmithconsulting/freehand:${FREEHAND_VERSION:-latest}"
    assert services["whatsapp-bridge"]["image"].startswith("ghcr.io/tracysmithconsulting/freehand-whatsapp-bridge:")
    # ...which the workflow publishes.
    assert "ghcr.io/tracysmithconsulting/${{ matrix.name }}" in WORKFLOW
    assert "name: freehand\n" in WORKFLOW and "name: freehand-whatsapp-bridge\n" in WORKFLOW


def test_the_other_services_are_the_same_pinned_images_as_for_developers():
    for name in ("open-connector", "hindsight"):
        assert INSTALL["services"][name]["image"] == DEV["services"][name]["image"], name
        assert "@sha256:" in INSTALL["services"][name]["image"]


def test_freehand_gets_everything_it_needs():
    want = {e.split("=", 1)[0] for e in DEV["services"]["freehand"]["environment"]}
    have = {e.split("=", 1)[0] for e in INSTALL["services"]["freehand"]["environment"]}
    # Hosted extras (Composio for Facebook and Instagram) are set up per client, not by installers.
    optional = {"COMPOSIO_API_KEY", "COMPOSIO_AUTH_CONFIG_FACEBOOK", "COMPOSIO_AUTH_CONFIG_INSTAGRAM",
                "COMPOSIO_BASE_URL"}
    assert want - optional <= have


def test_only_this_computer_can_reach_it():
    for name, service in INSTALL["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_the_published_images_leave_out_private_files():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for private in ("vault/", ".env", "**/auth_info_baileys/", "*.key"):
        assert private in ignored, private
    bridge_ignored = (ROOT / "freehand" / "bridge" / ".dockerignore").read_text(encoding="utf-8")
    assert "auth_info_baileys/" in bridge_ignored and ".env" in bridge_ignored
