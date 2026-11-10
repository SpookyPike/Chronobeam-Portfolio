"""Same-origin serving for an optional compiled frontend distribution."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


def mount_frontend(app: FastAPI, dist: Path) -> None:
    """Mount built assets and the HTML-navigation SPA fallback when available."""
    if not dist.is_dir():
        return

    dist_root = dist.resolve()
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets, html=False), name="frontend-assets")

    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_fallback(request: Request, path: str):
        protected = (
            path == "api"
            or path.startswith("api/")
            or path.startswith("health")
            or path == "openapi.json"
            or path == "docs"
            or path.startswith("docs/")
            or path == "redoc"
            or path.startswith("redoc/")
        )
        # Vite copies public files such as ``logo.png`` to the distribution
        # root rather than ``assets/``.  Serve those same-origin files too;
        # otherwise the production SPA's branding/favicon requests become
        # JSON 404s even though the compiled application is present.
        candidate = (dist_root / path).resolve()
        if not protected and path and candidate.is_relative_to(dist_root) and candidate.is_file():
            return FileResponse(candidate)
        accepts_html = "text/html" in request.headers.get("accept", "").lower()
        if index.is_file() and accepts_html and not protected:
            return FileResponse(index)
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
