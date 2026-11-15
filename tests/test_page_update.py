"""A FreeHand tab left open across an update notices, and offers to reload
(Nov 2026). Before, an old page kept running old code, so fixes seemed
not to work (the email form opened a sign-in window instead)."""
import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def test_the_page_knows_its_own_version(client):
    import server
    page = client.get("/")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    version = client.get("/api/page-version").json()["version"]
    assert len(version) == 12
    assert f"var mine = '{version}';" in page.text
    assert server.PAGE_VERSION_MARK not in page.text


def test_a_changed_page_is_a_new_version(client, monkeypatch):
    import server
    before = client.get("/api/page-version").json()["version"]
    real = server._page

    def updated():
        text, _ = real()
        import hashlib
        text += "<!-- an update -->"
        return text, hashlib.sha256(text.encode()).hexdigest()[:12]
    monkeypatch.setattr(server, "_page", updated)
    assert client.get("/api/page-version").json()["version"] != before


def test_the_notice_is_announced_and_never_reloads_by_itself(client):
    page = client.get("/").text
    notice = page[page.index('id="update-notice"') - 80:page.index('id="update-later"')]
    assert 'role="alert"' in notice and "hidden" in notice
    assert "Reload now" in notice and "Later" in page
    script = page[page.index("Was FreeHand updated while this page was open"):]
    script = script[:script.index("// ---- Status Region ----")]
    assert script.count("location.reload()") == 1 and "onclick" in script  # only when asked
