"""Static-file serving for the Cypher Workbench UI.

Two responsibilities:

1. :func:`_check_ui_dist_freshness` — startup-time probe that compares
   ``ui/dist/`` against ``ui/src/`` and emits a WARNING when the
   bundle is older than the source tree. Closes the operator-confusion
   case captured on 2026-04-24 where the visible UI was a bundle
   ~10 hours older than ``ui/src/App.tsx``.
2. The legacy ``/ui`` mount + the AMP ``/frontend`` mount + the
   shared ``/assets`` immutable mount + per-icon GET routes. Both
   mounts share the same cache-headers contract (HTML revalidates,
   hashed Vite assets get a one-year ``immutable`` policy).

Imported last by the package init so the freshness-check WARNING
appears after every endpoint registration log line — that ordering
is intentional and pinned by reading the startup log when triaging
"why is my UI stale" tickets.
"""

from __future__ import annotations

import logging as _logging
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .app import _svc_logger, app

# Climb out of arango_cypher/service/ui.py → arango_cypher/service/ →
# arango_cypher/ → repo root. Keeps the resolved paths byte-equivalent
# to the pre-split flat-file layout — pinned by
# tests/test_service.py::TestUiCacheHeaders which fails closed (skip)
# rather than open if these point at a non-existent dir.
_UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"
_UI_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "ui" / "src"


def _check_ui_dist_freshness(
    dist_dir: Path = _UI_DIR,
    src_dir: Path = _UI_SRC_DIR,
    *,
    logger: _logging.Logger | None = None,
) -> str:
    """Compare ``ui/dist`` against ``ui/src`` and emit a startup log line.

    Returns one of ``"ok"`` / ``"stale"`` / ``"missing"`` / ``"no_src"``
    so callers (and tests) can assert on the outcome without having to
    parse the log stream. The actual log emission is the side effect:

    * ``missing`` — ``ui/dist`` does not exist or has no ``index.html``;
      the UI mount block immediately below this function will skip
      registration entirely. We log a WARNING instead of staying silent
      so a fresh-clone operator who hits ``GET /ui`` and sees a 404
      knows to run the build, not to file a bug.
    * ``stale`` — the newest ``.tsx`` / ``.ts`` / ``.css`` / ``index.html``
      mtime under ``ui/src`` (and ``ui/index.html``) is *more recent*
      than ``ui/dist/index.html``. The bundle will still be served (so
      we don't break demos at startup), but the operator gets a clear
      WARNING + the exact ``cd ui && npm run build`` command. This is
      the case that bit us on 2026-04-24 (the visible "Cypher Workbench"
      title was a bundle ~10 hours older than ``ui/src/App.tsx``).
    * ``no_src`` — installed-from-wheel deployments have no ``ui/src``
      tree; freshness is undefined and we stay silent.
    * ``ok`` — silent. The vast majority of starts.

    The check is best-effort: any ``OSError`` during mtime probing
    short-circuits to ``"ok"`` rather than crashing the service boot.
    """
    log = logger or _svc_logger
    dist_index = dist_dir / "index.html"

    if not dist_index.is_file():
        if src_dir.is_dir():
            log.warning(
                "UI bundle not built: %s is missing. Run `cd ui && npm run build` "
                "to populate it; until then, /ui and /frontend will return 404.",
                dist_dir,
            )
            return "missing"
        return "no_src"

    if not src_dir.is_dir():
        return "no_src"

    try:
        dist_mtime = dist_index.stat().st_mtime
        # Walk only the source files Vite actually consumes; node_modules /
        # build artefacts in ui/dist itself / .turbo etc. are uninteresting
        # and would make the check both slow and falsely "stale".
        newest_src_mtime = dist_mtime
        for ext in ("*.ts", "*.tsx", "*.css", "*.html"):
            for path in src_dir.rglob(ext):
                m = path.stat().st_mtime
                if m > newest_src_mtime:
                    newest_src_mtime = m
        # Top-level ui/index.html is the Vite entry shell — also drives the build.
        top_index = src_dir.parent / "index.html"
        if top_index.is_file():
            m = top_index.stat().st_mtime
            if m > newest_src_mtime:
                newest_src_mtime = m
    except OSError:
        return "ok"

    if newest_src_mtime > dist_mtime:
        from datetime import UTC, datetime

        drift_seconds = int(newest_src_mtime - dist_mtime)
        log.warning(
            "UI bundle is stale: %s is %d seconds older than the newest source "
            "file under %s (dist built %s, newest src %s). Run "
            "`cd ui && npm run build` to refresh, otherwise /ui will serve the "
            "previous build.",
            dist_index,
            drift_seconds,
            src_dir,
            datetime.fromtimestamp(dist_mtime, tz=UTC).isoformat(timespec="seconds"),
            datetime.fromtimestamp(newest_src_mtime, tz=UTC).isoformat(timespec="seconds"),
        )
        return "stale"

    return "ok"


_check_ui_dist_freshness()


if _UI_DIR.is_dir():
    # Cache policy:
    #   - index.html (the SPA shell) MUST always revalidate. Without this,
    #     Chrome's heuristic cache will pin a stale shell that keeps replaying
    #     stale `/connect` calls or pointing at an old hashed asset bundle,
    #     and the only fix is "Application → Clear site data" — exactly the
    #     situation this comment is meant to prevent.
    #   - /assets/* files are content-hashed by Vite, so they are safe to
    #     mark immutable for a year.
    _HTML_NO_CACHE = "no-cache, no-store, must-revalidate"
    _ASSET_IMMUTABLE = "public, max-age=31536000, immutable"

    def _html_response(path: Path) -> FileResponse:
        return FileResponse(path, headers={"Cache-Control": _HTML_NO_CACHE})

    def _spa_serve(full_path: str) -> FileResponse:
        """Serve a UI asset if it exists, otherwise fall back to index.html.

        Used by both the legacy ``/ui`` and AMP ``/frontend`` mounts so the
        cache-headers contract (HTML revalidates, hashed assets immutable) is
        identical across both prefixes — pinned by ``TestUiCacheHeaders``.
        """
        file = _UI_DIR / full_path
        if file.is_file():
            # Non-hashed files (e.g. an icon copied next to index.html) —
            # revalidate too. Hashed assets are served by the dedicated
            # _ImmutableAssets mount below at /assets.
            headers = {"Cache-Control": _HTML_NO_CACHE} if file.suffix == ".html" else None
            return FileResponse(file, headers=headers) if headers else FileResponse(file)
        return _html_response(_UI_DIR / "index.html")

    # Legacy mount: /ui (local dev, existing bookmarks). Kept alongside the
    # AMP /frontend mount below so backward-compat doesn't depend on a
    # follow-up sweep through every doc, runbook, or operator workflow.
    @app.api_route("/ui", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/ui/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _ui_index() -> FileResponse:
        return _html_response(_UI_DIR / "index.html")

    @app.api_route("/ui/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _ui_spa_fallback(full_path: str) -> FileResponse:
        return _spa_serve(full_path)

    # AMP mount: /frontend. Required by the ArangoDB platform proxy which
    # routes /frontend (not /ui) to the BYOC container. The bare /frontend
    # (no trailing slash) handler is critical: Starlette's default StaticFiles
    # mount issues a 307 redirect to /frontend/ which the AMP proxy does NOT
    # forward to the container, surfacing as a platform-level 404. We use
    # explicit handlers (not app.mount + StaticFiles) so we can apply the
    # same cache-headers contract as /ui without a StaticFiles subclass.
    @app.api_route("/frontend", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/frontend/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _frontend_index() -> FileResponse:
        return _html_response(_UI_DIR / "index.html")

    @app.api_route(
        "/frontend/{full_path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def _frontend_spa_fallback(full_path: str) -> FileResponse:
        return _spa_serve(full_path)

    # The Vite build emits root-relative URLs (`/assets/...`, `/favicon.svg`,
    # `/icons.svg`) to match its dev server (`port: 5173`, no `base: '/ui/'`).
    # Mount them at the app root so the production-mode `/ui` page can load
    # its JS / CSS / icons without a rebuild.
    _UI_ASSETS = _UI_DIR / "assets"
    if _UI_ASSETS.is_dir():

        class _ImmutableAssets(StaticFiles):
            """StaticFiles subclass that marks hashed Vite assets immutable."""

            async def get_response(self, path, scope):  # type: ignore[override]
                response = await super().get_response(path, scope)
                if response.status_code == 200:
                    response.headers["Cache-Control"] = _ASSET_IMMUTABLE
                return response

        app.mount(
            "/assets",
            _ImmutableAssets(directory=str(_UI_ASSETS)),
            name="ui_assets",
        )

    for _icon in ("favicon.svg", "icons.svg"):
        _icon_path = _UI_DIR / _icon
        if _icon_path.is_file():

            def _make_icon_route(path: Path):
                async def _serve_icon() -> FileResponse:
                    return FileResponse(path)

                return _serve_icon

            app.add_api_route(
                f"/{_icon}",
                _make_icon_route(_icon_path),
                include_in_schema=False,
            )
