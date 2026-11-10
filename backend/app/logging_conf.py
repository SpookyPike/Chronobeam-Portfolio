"""Logging configuration with sensitive-data redaction (DP-LOG-001, M6).

Our own code never logs request bodies, comments, reasons, tokens, or HR
payloads. The filter below is defense-in-depth: it scrubs known token/secret
patterns from ANY log record (including ``uvicorn.access``/``uvicorn.error``,
whose format strings are outside our control) before the record is emitted.
"""

import logging
import re
import traceback
from typing import Any

# Patterns that must never reach logs. Bearer tokens are the OIDC seam's
# credential; the dev principal header value is a pseudonymous id (still
# scrubbed — defense in depth). CHRONOBEAM_* values may embed connection
# strings containing passwords.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Query strings are stripped from a request line BEFORE the value patterns
    # below: no pattern can recognise an arbitrary free-text query value, and
    # several routes take one (`?reason=` on a day-status delete, for example),
    # which can carry medical or personal detail. `uvicorn.access` also records
    # the raw request path, which may contain employee IDs, so its target is
    # replaced with a fixed marker. The application request log records the
    # resolved route template instead.
    (
        re.compile(r'"(?P<method>[A-Z]+) [^"\s]*'),
        r'"\g<method> [REDACTED]',
    ),
    (re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(x-chronobeam-dev-principal[:\s=]+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password|passwd|pwd)([=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(authorization[:\s=]+bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:cookie|x-csrf-token)[:\s=]+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^\s:@]+:)[^@\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token|secret|api[_-]?key)([=:]\s*)\S+"), r"\1\2[REDACTED]"),
]


def scrub(text: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrubbed_args(args: Any) -> Any:
    """Scrub the string values inside a record's args, preserving their SHAPE.

    The shape is load-bearing. ``uvicorn``'s ``AccessFormatter`` unpacks the args
    positionally — ``(client_addr, method, full_path, http_version, status_code)``
    — so replacing the tuple with one pre-formatted message (the obvious way to
    scrub a record) makes every access record raise
    ``ValueError: not enough values to unpack`` inside ``emit``. ``logging``
    swallows that into stderr, so the access line is lost *and* the log fills
    with tracebacks. Patterns that span several args — the request line is split
    across three — are caught by ``ScrubbingFormatter.format``, which scrubs the
    whole rendered output.
    """
    if isinstance(args, dict):
        return {
            key: scrub(value) if isinstance(value, str) else value for key, value in args.items()
        }
    if isinstance(args, tuple):
        return tuple(scrub(value) if isinstance(value, str) else value for value in args)
    return scrub(args) if isinstance(args, str) else args


class SensitiveDataFilter(logging.Filter):
    """Redacts a record's message and args in place before emission.

    Args keep their shape (see ``_scrubbed_args``); a record without args has its
    message scrubbed directly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = _scrubbed_args(record.args)
        else:
            scrubbed = scrub(str(record.msg))
            if scrubbed != record.msg:
                record.msg = scrubbed
        # Logging extras can otherwise bypass format-string redaction.
        for key, value in tuple(record.__dict__.items()):
            secret_key = any(word in key.lower() for word in ("token", "secret", "cookie"))
            if isinstance(value, str) and secret_key:
                record.__dict__[key] = "[REDACTED]"
        return True


class ScrubbingFormatter(logging.Formatter):
    """Emit a scrubbed record whose traceback carries no raw payload.

    Two distinct leaks are closed here, and the second is why the exception
    message is withheld rather than merely pattern-scrubbed:

    1. **Patterns.** A ``Formatter`` renders ``exc_info`` *after* the filters
       have run, so traceback text structurally never passed through
       ``scrub()``. ``format`` scrubs the whole rendered output, which is what
       catches a token/DSN/cookie that appears only inside a traceback.
    2. **Bound values.** ``scrub()`` recognises *shapes* (bearer tokens,
       ``password=``, DSNs). A database error's message is the failing SQL with
       its bound values inlined — ``INSERT INTO work_interval (comment) VALUES
       ('…')`` — and no pattern can tell that value from ordinary text. So
       ``formatException`` keeps what is *code* (the frame locations and the
       exception class chain, which is what triage needs) and drops the message
       text, replacing it with an explicit marker. ``EINV-16`` says logs never
       emit raw payloads; withholding the message is the only honest way to
       keep that promise for arbitrary values.
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))

    def formatException(self, ei: Any) -> str:
        """Render frames + the exception class chain, never the message."""
        _exc_type, value, tb = ei
        frames = "".join(traceback.format_tb(tb))
        return (
            f"Traceback (most recent call last):\n{frames}"
            f"{_exception_class_chain(value)}: {_MESSAGE_WITHHELD}"
        )


#: Explicit, greppable marker for a withheld exception message.
_MESSAGE_WITHHELD = "[exception message withheld: EINV-16]"


def _exception_class_chain(value: BaseException) -> str:
    """``OuterError <- InnerError`` — class names only, never messages.

    Follows ``__cause__``/``__context__`` so a wrapped error stays readable.
    ``ceiling:`` only the top-level frames are rendered (the chain's own
    tracebacks are dropped); add per-cause ``format_tb`` calls if a wrapped
    error ever needs its own frames.
    """
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = value
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(names)


def _scrubbing_formatter(existing: logging.Formatter | None) -> logging.Formatter:
    """Wrap a handler's current formatter, preserving its class behaviour.

    Handlers configured elsewhere (uvicorn installs its own) must keep their
    output shape; only the scrubbing is added.

    Preserving the format STRING is not enough — the CLASS matters. uvicorn's
    ``ColourizedFormatter`` supplies ``levelprefix`` from an overridden
    ``formatMessage``, so rebuilding a plain ``logging.Formatter`` from its
    format string drops that override and every uvicorn record fails with
    ``ValueError: Formatting field not found in record: 'levelprefix'``. The
    record is then lost, which silently defeats the access log this module
    exists to scrub. Mixing ``ScrubbingFormatter`` in front of the existing
    class keeps its ``formatMessage`` and puts our ``format``/
    ``formatException`` first in the MRO.
    """
    if isinstance(existing, ScrubbingFormatter):
        return existing
    base: type[logging.Formatter] = (
        type(existing) if isinstance(existing, logging.Formatter) else logging.Formatter
    )
    combined = type(f"Scrubbing{base.__name__}", (ScrubbingFormatter, base), {})
    fmt = getattr(existing, "_fmt", None) or "%(levelname)s %(name)s: %(message)s"
    datefmt = getattr(existing, "datefmt", None)
    return combined(fmt, datefmt=datefmt)


def configure_logging(level: str = "INFO") -> None:
    configured_level = getattr(logging, level.upper(), logging.INFO)
    if not logging.root.handlers:
        logging.basicConfig(level=configured_level, format="%(levelname)s %(name)s: %(message)s")
    logging.root.setLevel(configured_level)
    redactor = SensitiveDataFilter()
    # Filters on handlers cover records propagated from every named logger,
    # rather than relying on our application logger names alone.
    for handler in logging.root.handlers:
        handler.addFilter(redactor)
        handler.setFormatter(_scrubbing_formatter(handler.formatter))
    for logger_name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "app"):
        logging.getLogger(logger_name).propagate = True
    # uvicorn installs its own handlers (outside our format-string control) and
    # a record emitted by one of them never reaches the root handlers above, so
    # those need the same scrubbing or a traceback would escape through them.
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(logger_name).handlers:
            handler.addFilter(redactor)
            handler.setFormatter(_scrubbing_formatter(handler.formatter))
