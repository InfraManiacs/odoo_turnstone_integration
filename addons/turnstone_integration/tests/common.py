"""Fixtures for the Turnstone integration addon's tests.

WHY THIS FILE EXISTS

Every test in this addon has the same three problems, and each one has a way of
failing that looks like a bug in the code under test:

1. it needs a `turnstone.connection`, a `turnstone.agent` and users with the
   right groups, or it tests the wrong thing (an AccessError instead of the
   behaviour it meant to cover);
2. it must talk to the **mock** Turnstone server and never to a real one — a
   test that quietly reaches a live console would read somebody's sessions and
   could steer a real agent;
3. the mock's state is process-wide and survives between tests, so a test that
   assumes an empty server passes on its own and fails in a suite.

`TurnstoneTransactionCase` answers all three: it builds the records, it pins
every outgoing `urllib` request to the mock's host, and it resets the mock
before each test.

THE MOCK IS ADDRESSED THROUGH THE ENVIRONMENT, NOT HARD-CODED

`scripts/test-odoo.sh` + `docker-compose.test.yml` pass the mock's URL and
token in as `OTI_MOCK_TURNSTONE_URL` and `MOCK_TURNSTONE_TOKEN`. The token is
generated per run, so a hard-coded one cannot work, and the URL differs between
compose (`http://mock-turnstone:8099`) and a host run
(`http://localhost:8099`) — see `testing/mock-turnstone-harness.md`.

THE URL MUST NOT INCLUDE `/v1/api`

`TurnstoneClient.__init__` appends it, so a base URL that already carries it
turns every path into `/v1/api/v1/api/...` and answers 404. This module refuses
such a URL at import rather than letting it surface as a confusing 404 in a
test.
"""

import json
import logging
import os
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen as _real_urlopen

from odoo.tests import TransactionCase, new_test_user

_logger = logging.getLogger(__name__)

MOCK_URL_ENV = "OTI_MOCK_TURNSTONE_URL"
MOCK_TOKEN_ENV = "MOCK_TURNSTONE_TOKEN"

# The mock's own defaults (docker/mock-turnstone/server.py), used when the
# environment says nothing: a run started by hand against a mock started by
# hand with no environment set. The compose run always sets both.
DEFAULT_MOCK_URL = "http://mock-turnstone:8099"
DEFAULT_MOCK_TOKEN = "mock-token"

#: How long a fixture call may take before it is called a failure. Short on
#: purpose: an unreachable mock must fail a test rather than hang the run, and
#: a test that waits 30s per call looks like a hang, not a failure.
DEFAULT_TIMEOUT = 10


def _read_mock_url():
    """The mock server's base URL, validated once at import."""
    raw = (os.environ.get(MOCK_URL_ENV) or "").strip() or DEFAULT_MOCK_URL
    parts = urlsplit(raw.rstrip("/"))
    if parts.scheme not in ("http", "https"):
        raise AssertionError(
            "%s=%r is not an http(s) URL." % (MOCK_URL_ENV, raw)
        )
    if not parts.hostname:
        raise AssertionError("%s=%r has no host." % (MOCK_URL_ENV, raw))
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise AssertionError(
            "%s=%r must be a bare base URL: TurnstoneClient appends /v1/api "
            "itself, so a path here produces /v1/api/v1/api/... and a 404 on "
            "every call." % (MOCK_URL_ENV, raw)
        )
    return "%s://%s" % (parts.scheme, parts.netloc)


MOCK_BASE_URL = _read_mock_url().rstrip("/")
MOCK_TOKEN = os.environ.get(MOCK_TOKEN_ENV) or DEFAULT_MOCK_TOKEN

_parts = urlsplit(MOCK_BASE_URL)
MOCK_HOST = _parts.hostname
MOCK_PORT = _parts.port or (443 if _parts.scheme == "https" else 80)
#: The one (host, port) pair a test may open a socket to.
MOCK_AUTHORITY = (MOCK_HOST, MOCK_PORT)


def is_mock_url(url):
    """Whether `url` addresses the pinned mock server.

    Compares the **authority**, not the host alone: `mock-turnstone:8099` is
    the mock, and a real console happens to share nothing with it, but a stray
    `:80` on the same name would still be a different server.
    """
    parts = urlsplit(url)
    if parts.scheme == "file":
        # No socket, so nothing can be reached. Odoo's own report code
        # urlopen()s file: URLs and this guard is about the network.
        return True
    default_port = 443 if parts.scheme == "https" else 80
    return (parts.hostname, parts.port or default_port) == MOCK_AUTHORITY


def _mock_only_urlopen(url, *args, **kwargs):
    """`urllib.request.urlopen`, refusing every host but the mock.

    Installed for the duration of a test class. It is patched at the module
    attribute (`urllib.request.urlopen`), which is where `turnstone_client`
    looks it up at call time — so it covers the addon's own client, this
    fixture's control-plane calls, and any future code that forgets.

    Raising `AssertionError` rather than `UserError` is deliberate: the addon
    turns `URLError`/`HTTPError` into a friendly message, and this must NOT be
    friendly. A test that reaches a non-mock host is a broken test, and the
    traceback should point at it.
    """
    target = url.full_url if isinstance(url, Request) else str(url)
    if not is_mock_url(target):
        raise AssertionError(
            "Refusing a real network request to %s. Tests are pinned to the mock "
            "Turnstone server at %s (host:port %s:%s, set %s to change it). A test "
            "must never contact a real Turnstone server."
            % (target, MOCK_BASE_URL, MOCK_HOST, MOCK_PORT, MOCK_URL_ENV)
        )
    return _real_urlopen(url, *args, **kwargs)


class MockTurnstone:
    """The mock server's control plane, as a small client.

    A `TransactionCase` runs inside the Odoo container and cannot import the
    mock's `state.py`, so the fixtures reach it over HTTP — `/v1/api/admin/mock/
    {reset,seed,state,requests,routes}`, which are the mock's own paths and not
    Turnstone's (see `testing/mock-turnstone-harness.md`).

    Every method here goes through `_call`, which uses the same pinned URL as
    the fixture, so a seed call cannot land on a different server than the one
    the addon is talking to.
    """

    def __init__(self, base_url=None, token=None, timeout=DEFAULT_TIMEOUT):
        self.base_url = (base_url or MOCK_BASE_URL).rstrip("/")
        self.token = token or MOCK_TOKEN
        self.timeout = timeout
        if not is_mock_url(self.base_url):
            raise AssertionError(
                "%s addresses %s, which is not the pinned mock server (%s:%s)."
                % (self.__class__.__name__, self.base_url, MOCK_HOST, MOCK_PORT)
            )
        self.root = self.base_url + "/v1/api/admin/mock"

    # -- transport ---------------------------------------------------------

    def _call(self, method, leaf, payload=None, params=None):
        url = self.root + leaf
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer %s" % self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with _real_urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except HTTPError as exc:
            body = exc.read().decode()[:500]
            raise AssertionError(
                "The mock Turnstone server refused %s %s with HTTP %s: %s\n"
                "Is %s the token the mock was started with (%s), and is the "
                "server the one this run started?"
                % (method, url, exc.code, body, MOCK_TOKEN_ENV, MOCK_TOKEN_ENV)
            ) from exc
        except URLError as exc:
            raise AssertionError(
                "Cannot reach the mock Turnstone server at %s (%s).\n"
                "Run the tests through scripts/test-odoo.sh, which starts it and "
                "passes %s and %s to the Odoo container."
                % (url, exc.reason, MOCK_URL_ENV, MOCK_TOKEN_ENV)
            ) from exc
        return json.loads(raw) if raw.strip() else {}

    # -- the control plane -------------------------------------------------

    def health(self):
        """Liveness. Fails fast when the harness is not running."""
        req = Request(
            self.base_url + "/health",
            headers={"Authorization": "Bearer %s" % self.token},
        )
        try:
            with _real_urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except (HTTPError, URLError) as exc:
            raise AssertionError(
                "The mock Turnstone server is not reachable at %s (%s).\n"
                "Run the tests through scripts/test-odoo.sh, which starts it and "
                "passes %s and %s to the Odoo container."
                % (self.base_url, exc, MOCK_URL_ENV, MOCK_TOKEN_ENV)
            ) from exc

    def reset(self):
        """Empty the mock. Called before every test by the base case."""
        return self._call("POST", "/reset")

    def state(self):
        """Everything the mock holds: `{workstreams, skills, personas, ...}`."""
        return self._call("GET", "/state")

    def workstreams(self):
        """Every workstream on the mock, oldest first."""
        return self.state().get("workstreams") or []

    def workstream_ids(self):
        """Just the ids — what a "did this launch twice" assertion reads."""
        return [row["id"] for row in self.workstreams()]

    def seed(self, kind="interactive", **fields):
        """Plant a workstream, and optionally a whole transcript, in one call.

        `kind` is required by the mock; `ws_id` pins the id so a test can name
        it instead of threading a returned value around, and `messages` plants
        the transcript before anything opens the session. Returns the stored
        record — assert on what was stored, not on what was sent, because the
        mock normalises message rows (`testing/turnstone-transcript-contract.md`).

        A workstream that is already on the mock is a 409 rather than a
        replacement, and the pinned id is skipped by the id generator, so a
        later create cannot collide with a seeded one. Call `reset()` (which
        `setUp` already does) before seeding an id a previous test used.
        """
        return self._call("POST", "/seed", dict(fields, kind=kind))

    def seed_transcript(self, ws_id, messages, kind="interactive", **fields):
        """Shorthand for `seed` with a pinned id and a transcript."""
        return self.seed(kind=kind, ws_id=ws_id, messages=messages, **fields)

    def request_log(self, path_prefix=None, method=None, limit=100):
        """What the mock received, newest last. Never records the token."""
        params = {"limit": limit}
        if path_prefix:
            params["path_prefix"] = path_prefix
        if method:
            params["method"] = method
        return self._call("GET", "/requests", params=params).get("requests") or []

    def routes(self):
        """The mock's live route table, with `implemented`/`owner` per entry."""
        return self._call("GET", "/routes")


class TurnstoneTransactionCase(TransactionCase):
    """The base case every test in this addon uses.

    Creates, once per class:

    * ``admin_user`` — a Turnstone **Administrator** (implies Turnstone User),
      the only group that may read a connection's token or configure servers;
    * ``user`` — an ordinary **Turnstone User**, which is who starts sessions;
    * ``outsider`` — an internal user with **no** Turnstone group, so an
      authorization test has somebody to be refused;
    * ``connection`` — pointed at the mock, default for the company;
    * ``agent`` — an interactive agent on that connection.

    And it wraps every test so that:

    * only the mock's host can be reached (`urllib.request.urlopen` refuses
      anything else, immediately and loudly);
    * the mock is reset in `setUp`, so no test inherits another's workstreams.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Pin the network BEFORE anything makes a request, so a mistake here is
        # caught by the fixture itself rather than by a real server answering.
        cls.startClassPatcher(patch("urllib.request.urlopen", _mock_only_urlopen))

        cls.mock = MockTurnstone()
        # Fail fast: if the harness is not up there is nothing to test against,
        # and every test would fail later with a much worse message.
        health = cls.mock.health()
        if not health.get("mock"):
            raise AssertionError(
                "%s answered, but it does not identify as the mock Turnstone "
                "server (no `mock` marker). Refusing to run tests against it."
                % MOCK_BASE_URL
            )

        cls.admin_user = new_test_user(
            cls.env,
            login="turnstone-admin",
            name="Turnstone Administrator",
            groups="base.group_user,turnstone_integration.group_turnstone_manager",
        )
        cls.user = new_test_user(
            cls.env,
            login="turnstone-user",
            name="Turnstone User",
            groups="base.group_user,turnstone_integration.group_turnstone_user",
        )
        cls.outsider = new_test_user(
            cls.env,
            login="turnstone-outsider",
            name="No Turnstone Access",
            groups="base.group_user",
        )

        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Mock Turnstone",
            "base_url": MOCK_BASE_URL,
            "api_token": MOCK_TOKEN,
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Mock Agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })

    def setUp(self):
        super().setUp()
        self.mock.reset()

    # -- helpers tests build on --------------------------------------------

    def as_user(self, user):
        """An environment bound to `user`, so ACLs and record rules apply.

        Prefer this over `self.env = ...`: the base class keeps other
        bookkeeping in `self.env`, and an environment that is swapped in one
        test and left behind is a classic source of "passes alone, fails in the
        suite".
        """
        return self.env(user=user)

    def make_connection(self, **vals):
        """A second connection, for a test that needs two servers."""
        values = {"name": "Second Mock Turnstone", "base_url": MOCK_BASE_URL,
                  "api_token": MOCK_TOKEN}
        values.update(vals)
        return self.env["turnstone.connection"].create(values)

    def make_agent(self, **vals):
        """Another agent on the fixture's connection."""
        values = {"name": "Second Mock Agent", "connection_id": self.connection.id,
                  "kind": "interactive"}
        values.update(vals)
        return self.env["turnstone.agent"].create(values)

    def make_session(self, user=None, **vals):
        """A session owned by `user` (default: the ordinary Turnstone User).

        Created through that user's own environment, because the interesting
        failures in this module are about what a user may do — creating the row
        as SUPERUSER and then asserting on it tests nothing.
        """
        user = user or self.user
        values = {
            "name": "Fixture session",
            "connection_id": self.connection.id,
            "agent_id": self.agent.id,
        }
        values.update(vals)
        return self.as_user(user)["turnstone.session"].create(values)
