"""Same-origin compiled SPA serving remains opt-in and API-safe."""

from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


def _client_for_dist(monkeypatch, dist) -> TestClient:
    monkeypatch.setenv("CHRONOBEAM_ENV", "test")
    monkeypatch.setenv("CHRONOBEAM_FRONTEND_DIST", str(dist))
    get_settings.cache_clear()
    return TestClient(create_app())


def test_spa_static_assets_and_html_fallback(monkeypatch, tmp_path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<main>Chronobeam SPA</main>", encoding="utf-8")
    (dist / "assets" / "x.js").write_text("console.log('x')", encoding="utf-8")
    (dist / "logo.png").write_bytes(b"synthetic-logo")
    with _client_for_dist(monkeypatch, dist) as client:
        root = client.get("/", headers={"Accept": "text/html"})
        assert root.text == "<main>Chronobeam SPA</main>"
        assert client.get("/assets/x.js").text == "console.log('x')"
        assert client.get("/logo.png").content == b"synthetic-logo"
        api = client.get("/api/v1/definitely-missing", headers={"Accept": "text/html"})
        assert api.status_code == 404
        assert "Chronobeam SPA" not in api.text
        assert client.get("/health/live").status_code == 200
    get_settings.cache_clear()


def test_no_frontend_dist_leaves_root_as_api_404(monkeypatch, tmp_path) -> None:
    with _client_for_dist(monkeypatch, tmp_path / "absent") as client:
        response = client.get("/", headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
    get_settings.cache_clear()
