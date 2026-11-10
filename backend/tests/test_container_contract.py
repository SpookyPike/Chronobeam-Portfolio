"""Production image reproducibility and least-runtime contract."""

from pathlib import Path


def test_production_base_images_are_content_addressed() -> None:
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    source = dockerfile.read_text(encoding="utf-8")
    for image in (
        "node:22-bookworm-slim",
        "ghcr.io/astral-sh/uv:0.12.3",
        "python:3.12-slim-bookworm",
    ):
        assert f"FROM {image}@sha256:" in source
