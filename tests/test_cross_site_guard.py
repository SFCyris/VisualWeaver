# SPDX-License-Identifier: AGPL-3.0-or-later
"""There is no login, so a page on another site must not be able to drive the API from
the user's browser: no chats over the WebSocket, no writes, no background loads. The
app's own page, plain links, and non-browser clients (no Origin) keep working."""
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def client(app_module):
    from visualweaver import appcore
    return TestClient(appcore.app)


EVIL = {"Origin": "http://evil.example"}
SELF = {"Origin": "http://testserver"}


def test_writes_from_another_site_are_refused(client):
    for path in ("/api/config/import", "/api/sessions/x/fork", "/api/redis/test"):
        r = client.post(path, headers=EVIL, json={})
        assert r.status_code == 403 and r.json() == {"detail": "Cross-site request refused"}, path
    assert client.delete("/api/sessions/x", headers=EVIL).status_code == 403
    # Origin: null (sandboxed frames, file://) is not trusted for writes either
    assert client.post("/api/redis/test", headers={"Origin": "null"}, json={}).status_code == 403


def test_the_apps_own_page_and_non_browser_clients_still_work(client):
    assert client.delete("/api/sessions/x", headers=SELF).status_code == 200
    assert client.delete("/api/sessions/x").status_code == 200                     # curl: no Origin
    assert client.delete("/api/sessions/x", headers={"Origin": "http://TestServer:80"}).status_code == 200
    assert client.get("/api/health", headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}).status_code == 200


def test_background_loads_from_another_site_are_refused_but_links_work(client):
    # <img>/<script>/no-cors fetch from another site: no Origin, but Sec-Fetch-Site says so
    r = client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})
    assert r.status_code == 403
    # a CORS fetch from another site carries its Origin
    assert client.get("/api/health", headers={**EVIL, "Sec-Fetch-Mode": "cors"}).status_code == 403
    # following a link from another site to the app is a navigation, which is allowed
    r = client.get("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    assert r.status_code == 200


def test_the_chat_websocket_refuses_another_sites_page(client):
    code = None
    try:
        with client.websocket_connect("/ws/chat/guardtest", headers=EVIL) as ws:
            ws.close()             # accepted: fail below, without waiting for a message
    except WebSocketDisconnect as e:
        code = e.code
    assert code == 1008, "another site's page was allowed to open the chat WebSocket"
    with client.websocket_connect("/ws/chat/guardtest", headers=SELF) as ws:   # own page: accepted
        ws.close()


def test_listed_origins_are_trusted(client, monkeypatch):
    from visualweaver import appcore
    monkeypatch.setattr(appcore, "_cors_origins", ["https://ui.example/"])
    assert client.delete("/api/sessions/x", headers={"Origin": "https://ui.example"}).status_code == 200
    assert client.delete("/api/sessions/x", headers=EVIL).status_code == 403


def test_an_invalid_config_import_is_a_400_not_a_500(client):
    for body in (b"{not json", b"[1, 2]", b"\xff\xfe"):
        r = client.post("/api/config/import", files={"file": ("c.json", body, "application/json")})
        assert r.status_code == 400 and "Not a VisualWeaver config file" in r.json()["detail"], (body, r.text)


def test_dns_rebinding_host_names_are_refused_for_browsers(client, monkeypatch):
    from visualweaver import appcore
    page = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    # an attacker's domain re-pointed at this machine: same-origin for the browser
    r = client.get("/api/sessions", headers={**page, "Host": "rebind.attacker.example:8420"})
    assert r.status_code == 403 and "VISUALWEAVER_ALLOWED_HOSTS=rebind.attacker.example" in r.text
    r = client.get("/", headers={"Host": "rebind.attacker.example:8420", "Sec-Fetch-Mode": "navigate",
                                 "Sec-Fetch-Site": "none"})
    assert r.status_code == 403
    # names an outsider cannot own still work
    for host in ("127.0.0.1:8420", "192.168.1.20:8420", "[::1]:8420", "localhost:8420", "nas:8420",
                 "my-mac.local:8420", "vw.home.arpa", "box.lan:8420"):
        assert client.get("/api/health", headers={**page, "Host": host}).status_code == 200, host
    # a reverse proxy's domain, once listed (also as a wildcard)
    monkeypatch.setattr(appcore, "_allowed_hosts", {"vw.corp.example"})
    assert client.get("/api/health", headers={**page, "Host": "vw.corp.example"}).status_code == 200
    monkeypatch.setattr(appcore, "_allowed_hosts", {"*.corp.example"})
    assert client.get("/api/health", headers={**page, "Host": "a.corp.example"}).status_code == 200
    assert client.get("/api/health", headers={**page, "Host": "corp.example.attacker.example"}).status_code == 403
    # non-browser clients (no Origin, no Sec-Fetch-*) are not affected
    monkeypatch.setattr(appcore, "_allowed_hosts", set())
    assert client.get("/api/health", headers={"Host": "rebind.attacker.example"}).status_code == 200
