# Third-party notices

This repository contains no vendored third-party source code, no bundled fonts, no third-party
images and no external runtime assets. The only bundled asset is the project's own logo
(`frontend/public/logo.png`), which is original work by the copyright holder.

The browser build does bundle JavaScript packages. Their notices ship with the build as
`frontend/public/third-party-notices.txt`, which Vite copies to `dist/third-party-notices.txt`, so
the file travels with every published SPA and every container image. Regenerate it after a
dependency change:

```bash
cd frontend && pnpm install --frozen-lockfile
cd .. && backend/.venv/bin/python scripts/third_party_notices.py
```

Dependencies are resolved at install time and remain under their own licenses:

| Ecosystem | Manifest | Licenses |
|---|---|---|
| Python | `backend/pyproject.toml`, `backend/uv.lock` | Mostly MIT, BSD and Apache-2.0. A dependency's license is authoritative for that dependency. |
| Node.js | `frontend/package.json`, `frontend/pnpm-lock.yaml` | Mostly MIT, ISC and Apache-2.0. A dependency's license is authoritative for that dependency. |
| Container base images | `Dockerfile`, `compose.yaml` | Debian and PostgreSQL images keep their own notices and licenses. |

## Obligations when redistributing

If you build or redistribute a deployment of this software, you are responsible for:

1. shipping the license texts of every dependency in your build,
2. honouring any copyleft or attribution term that applies to a dependency you include, and
3. keeping the copyright notice in `LICENSE.md` intact.

Nothing in this file grants rights to the software itself. That is governed by `LICENSE.md`.

## Generating the current inventory

```bash
# Python: install the reporting tool first; it is not a project dependency.
uv tool run pip-licenses --from=mixed --format=markdown --with-urls

# Node.js
cd frontend && pnpm licenses list
```