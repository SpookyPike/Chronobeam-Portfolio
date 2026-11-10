"""CUTOVER-T03: no production historical-workbook upload/import surface.

The Excel extractors are offline regression tooling under the tests tree
only. This test locks the boundary from both sides: the generated OpenAPI
document contains no workbook upload/import operation, the frontend exposes
no such route or API call, and the cutover seed endpoint is fail-closed in
production configuration.
"""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKBOOK_IMPORT_PATTERN = re.compile(r"(upload|import).*(workbook|xlsx|excel)", re.IGNORECASE)
FORBIDDEN_PATH_HINTS = ("upload", "/import", "workbook", ".xlsx")


def test_openapi_exposes_no_workbook_upload_or_import(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    offending = []
    for path, operations in schema["paths"].items():
        if any(hint in path.lower() for hint in FORBIDDEN_PATH_HINTS):
            offending.append(path)
        for operation in operations.values():
            text = json.dumps(operation)
            if WORKBOOK_IMPORT_PATTERN.search(text):
                offending.append(f"{path}:{operation.get('operationId')}")
    assert offending == []
    # The cutover seam exists and is the only legacy-boundary write surface.
    assert "/api/v1/cutover/opening-balances" in schema["paths"]


def test_frontend_has_no_workbook_upload_surface() -> None:
    sources = REPO_ROOT / "frontend" / "src"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(sources.rglob("*.ts*"))
        if WORKBOOK_IMPORT_PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_cutover_endpoint_fail_closed_in_production(production_client: TestClient) -> None:
    response = production_client.post(
        "/api/v1/cutover/opening-balances",
        headers={"X-Chronobeam-Dev-Principal": "cutover-operator"},
        json={"employee_external_id": "emp-0001", "cutover_month": "2026-08", "minutes": 10},
    )
    # The configured production IdP never treats a dev header as identity.
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
    assert production_client.get("/openapi.json").status_code == 404
    assert production_client.get("/docs").status_code == 404
