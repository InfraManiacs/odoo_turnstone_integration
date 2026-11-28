"""Taking credentials out of text that is about to be logged or stored.

WHY THIS IS A MODULE AND NOT A METHOD

The same credential turns up in two places that have nothing else in common:

* `turnstone.automation.run.error` — a stored, readable-by-more-people-than-a-
  connection column, cleaned by `_clean_text()`;
* an HTTP client's warning line — written before any Odoo record exists, on
  every failing call, cleaned by `TurnstoneClient._redact()`.

Duplicating the patterns would let one copy drift and quietly re-open the leak
the other one closed, so both import from here. It is deliberately a plain
module of functions: **it calls no `_()`**. A module-level helper that
translates logs "no translation language detected, skipping translation" with a
full stack trace on every call (`odoo/tools/translate.py::_get_lang` looks for
`self.env` on the *calling frame* — see `architecture/session-create-boundary.md`),
and the client has no env to offer. The replacement text is a fixed token, not a
sentence, so there is nothing to translate.

WHAT IS AND IS NOT REDACTED

Two layers, because neither alone is enough:

1. **Explicit secrets** — the token the caller just used. This is the only layer
   that catches a value printed in a shape no pattern could recognise, so a
   caller that knows the secret must pass it.
2. **Shapes** — a credential printed by a framework or a proxy: an
   `Authorization` header, a `Bearer` token, an `api_token=` assignment, a
   `?token=` query parameter. These catch the token in text nobody anticipated
   (a 500 body echoing the request is exactly that).

Patterns replace the *value* and keep the *name* (`token=[redacted]`), because a
log line whose credential is missing but whose shape is visible is still
diagnosable, while a blanked-out line is not.
"""

import re

#: What a credential is replaced with. Fixed text, never translated: the
#: replacement is the same in every log line and every language.
REDACTED = "[redacted]"

#: Order matters. A query parameter and an `api_token=` assignment are replaced
#: at their value first, so the broader `Authorization` pattern below cannot
#: swallow `&next=…` along with the secret it was aiming at.
REDACTION_PATTERNS = (
    # `?token=…`, `&api_token=…`, `&access_token=…` — the name is kept, up to
    # the next separator, quote or angle bracket. A tool that prints the URL it
    # called puts its credential here.
    re.compile(
        r"(?i)\b((?:api[_-]?token|api[_-]?key|access[_-]?token|token)=)[^\s&\"'<>]+"
    ),
    # `api_token=…`, `api_token: …`, `api-token: …` anywhere, not only in a URL.
    re.compile(r"(?i)\b((?:api[_-]?token|api[_-]?key|access[_-]?token)\s*[=:]\s*)\S+"),
    # A whole `Authorization:` header: the scheme and the credential after it.
    re.compile(r"(?i)\b(Authorization\b\s*[:=]\s*)\S+(?:\s+\S+)?"),
    # A bare `Bearer …` with no header name in front of it.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
)


def _substitute(match):
    """Keep whatever the pattern captured (the credential's name), drop the value."""
    return (match.group(1) if match.groups() else "") + REDACTED


def redact(text, secrets=()):
    """`text` with every known secret and every credential shape replaced.

    Idempotent: applying it twice gives the same string as applying it once, so
    a caller one layer up can clean text that was already cleaned without
    turning `[redacted]` into a different marker.
    """
    text = str(text or "")
    for secret in secrets or ():
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in REDACTION_PATTERNS:
        text = pattern.sub(_substitute, text)
    return text
