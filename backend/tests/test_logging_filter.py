"""Sensitive-data redaction (DP-LOG-001)."""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import cast

from app.admin.routes import router as admin_router
from app.config import get_settings
from app.db import get_engine
from app.logging_conf import ScrubbingFormatter, SensitiveDataFilter, configure_logging, scrub
from app.main import create_app
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match


def test_scrub_bearer_token() -> None:
    text = "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.sig-abc-123"
    scrubbed = scrub(text)
    assert "[REDACTED]" in scrubbed
    assert "eyJhbGci" not in scrubbed


def test_scrub_dev_principal_header() -> None:
    scrubbed = scrub("x-chronobeam-dev-principal: employee")
    assert "employee" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrub_password_assignments() -> None:
    scrubbed = scrub("password=sup3rsecret pwd= hunter2")
    assert "sup3rsecret" not in scrubbed
    assert "hunter2" not in scrubbed


def test_plain_text_untouched() -> None:
    text = "interval created ok"
    assert scrub(text) == text


def _record(message: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


def test_filter_scrubs_format_args() -> None:
    record = _record("request token=%s", ("Bearer abc.def",))
    assert SensitiveDataFilter().filter(record)
    assert "abc.def" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_filter_passes_clean_records() -> None:
    record = _record("GET /health 200")
    assert SensitiveDataFilter().filter(record)
    assert record.getMessage() == "GET /health 200"


def test_filter_scrubs_cookie_csrf_database_url_and_extra_token() -> None:
    record = _record(
        "Authorization: Bearer secret-token Cookie: __Host-Chronobeam-Session=xyz "
        "X-CSRF-Token: abc postgresql://user:pass@host/db"
    )
    record.session_token = "opaque-session"  # noqa: S105  # type: ignore[attr-defined]
    assert SensitiveDataFilter().filter(record)
    emitted = record.getMessage()
    for secret in ("secret-token", "xyz", "abc", "pass", "opaque-session"):
        assert secret not in emitted
    assert record.session_token == "[REDACTED]"  # noqa: S105  # type: ignore[attr-defined]


def test_EINV_16_formatted_log_output_never_contains_traceback_payload() -> None:
    """The redaction contract binds the FORMATTED output, not just the message.

    ``logger.exception`` attaches ``exc_info``; the formatter — not the filter —
    renders the traceback.  A database error's traceback carries the failing
    statement and its bound values, so the guarantee must be checked on the
    string that is actually emitted.
    """
    try:
        raise RuntimeError(
            "INSERT INTO work_interval (comment) VALUES ('private-victim-note') "
            "Authorization: Bearer secret-token"
        )
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="app.main",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="unexpected error while handling request",
        args=(),
        exc_info=exc_info,
    )
    assert SensitiveDataFilter().filter(record)
    rendered = ScrubbingFormatter("%(levelname)s %(name)s: %(message)s").format(record)
    for secret in ("private-victim-note", "secret-token"):
        assert secret not in rendered, f"formatted log leaked {secret!r}: {rendered!r}"
    # The fix must scrub the payload, NOT swallow the diagnostic: the exception
    # type has to stay discoverable or the log becomes useless for triage.
    assert "RuntimeError" in rendered
    assert "exception message withheld" in rendered


def test_configure_logging_installs_the_scrubbing_formatter_everywhere() -> None:
    """Wiring guard: the class is useless unless every reachable handler uses it.

    A handler left with a plain ``Formatter`` would render a traceback
    unscrubbed — including uvicorn's own handlers, which a record emitted by
    them never routes through the root handlers.
    """
    configure_logging("INFO")
    reachable = list(logging.root.handlers)
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        reachable.extend(logging.getLogger(logger_name).handlers)
    assert reachable, "no logging handler is configured at all"
    for handler in reachable:
        assert isinstance(handler.formatter, ScrubbingFormatter), (
            f"{handler!r} would emit an unscrubbed traceback"
        )


def test_scrub_strips_the_query_string_from_a_request_line() -> None:
    """`EINV-16` at the access log.

    ``uvicorn.access`` formats ``"%(request_line)s"``, so the query string is
    emitted verbatim. Several routes take a free-text query parameter
    (``?reason=`` on a day-status delete, for example) which can carry medical or
    personal detail, and no pattern can recognise an arbitrary value — so the
    target is removed outright because it may carry an employee identifier.
    """
    line = (
        'INFO:     127.0.0.1:51234 - "DELETE /api/v1/me/day-status/abc'
        '?version=1&reason=private-medical-note HTTP/1.1" 204'
    )
    scrubbed = scrub(line)
    assert "private-medical-note" not in scrubbed
    assert "reason=" not in scrubbed
    assert '"DELETE [REDACTED] HTTP/1.1"' in scrubbed
    assert "204" in scrubbed


def test_a_request_line_without_a_query_still_redacts_its_raw_target() -> None:
    line = 'INFO:     127.0.0.1:51234 - "GET /health/ready HTTP/1.1" 200'
    assert scrub(line) == 'INFO:     127.0.0.1:51234 - "GET [REDACTED] HTTP/1.1" 200'


def test_EINV_16_the_formatted_access_line_never_carries_a_query_value() -> None:
    """The guarantee binds the FORMATTED output, through the real formatter."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s" %s',
        args=(
            "127.0.0.1:51234",
            "DELETE /api/v1/me/day-status/abc?version=1&reason=private-medical-note HTTP/1.1",
            "204",
        ),
        exc_info=None,
    )
    assert SensitiveDataFilter().filter(record)
    rendered = ScrubbingFormatter("%(levelname)s %(name)s: %(message)s").format(record)
    assert "private-medical-note" not in rendered
    assert "reason=" not in rendered
    assert "/api/v1/me/day-status/abc" not in rendered
    assert "[REDACTED]" in rendered


def test_the_uvicorn_formatters_survive_scrubbing() -> None:
    """uvicorn's OWN handlers must still format after scrubbing is installed.

    uvicorn supplies `levelprefix` from an overridden `formatMessage`, so a
    scrubbing formatter that keeps only the format STRING drops it and every
    uvicorn record dies with `ValueError: Formatting field not found in record:
    'levelprefix'`. `logging` swallows that into stderr, so the access line this
    module exists to scrub is silently never emitted — found in CI, where the
    backend log was full of `--- Logging error ---` while the process stayed up.

    Emits through a real `AccessFormatter` and a real handler, because that is
    the only way the failure is observable: the exception is raised inside
    `emit`, not by `format` alone.

    The record also goes through the REDACTION FILTER first, which is the part
    that made this recur: scrubbing a record by inlining its args into the
    message and clearing `args` is correct for our own format strings but
    destroys the positional tuple `AccessFormatter` unpacks, so the access line
    died with `ValueError: not enough values to unpack (expected 5, got 0)` and
    `logging` swallowed it into stderr. Formatting a hand-built record that never
    met the filter cannot catch that.
    """
    import io

    from app.logging_conf import _scrubbing_formatter
    from uvicorn.logging import AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter(
            '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )
    )
    handler.setFormatter(_scrubbing_formatter(handler.formatter))

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:51234", "DELETE", "/api/v1/me/day-status/abc?reason=private", "1.1", 204),
        exc_info=None,
    )
    record.__dict__.update(
        client_addr="127.0.0.1:51234",
        request_line="DELETE /api/v1/me/day-status/abc?reason=private HTTP/1.1",
        status_code=204,
    )
    assert SensitiveDataFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5, (
        "the filter must not collapse the positional tuple"
    )
    handler.emit(record)

    rendered = stream.getvalue()
    # A formatting failure writes nothing here; it goes to stderr via handleError.
    assert rendered, "the record was dropped instead of formatted"
    assert "INFO:" in rendered, rendered  # uvicorn's levelprefix survived
    assert "private" not in rendered, rendered  # ...and the query value did not
    assert "DELETE [REDACTED]" in rendered, rendered


def test_request_logs_use_route_templates_and_hide_unmatched_paths(monkeypatch) -> None:
    monkeypatch.setenv("CHRONOBEAM_ENV", "test")
    get_settings.cache_clear()
    get_engine.cache_clear()
    app = create_app()
    middleware = next(
        item
        for item in app.user_middleware
        if getattr(item.kwargs.get("dispatch"), "__name__", None) == "_security_boundary"
    )
    dispatch = cast(
        Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]],
        middleware.kwargs["dispatch"],
    )
    admin_route = next(
        route
        for route in admin_router.routes
        if getattr(route, "path", None) == "/api/v1/admin/users/{employee_id}"
    )
    logger = logging.getLogger("app.request")
    previous_level = logger.level
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = CaptureHandler(level=logging.INFO)
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    sentinel = "0123456789ab4def8123456789abcdef"

    async def request(path: str, *, matched: bool) -> None:
        scope: dict[str, object] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
        request_object = Request(scope)

        async def call_next(request: Request) -> Response:
            if matched:
                match, child_scope = admin_route.matches(request.scope)
                assert match is Match.FULL
                request.scope.update(child_scope)
                request.scope["route"] = admin_route
            return Response(status_code=403 if matched else 404)

        await dispatch(request_object, call_next)

    try:
        asyncio.run(request(f"/api/v1/admin/users/{sentinel}", matched=True))
        matched_records = [record.getMessage() for record in records]
        assert matched_records
        assert any(
            "route=/api/v1/admin/users/{employee_id}" in message for message in matched_records
        )
        assert all(sentinel not in message for message in matched_records)

        records.clear()
        asyncio.run(request(f"/api/unmatched/{sentinel}", matched=False))
        unmatched_records = [record.getMessage() for record in records]
        assert unmatched_records
        assert any("route=UNMATCHED_ROUTE" in message for message in unmatched_records)
        assert all(sentinel not in message for message in unmatched_records)
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        get_settings.cache_clear()
        get_engine.cache_clear()
