"""Thin HTTP client for the Turnstone console API.

Everything this module knows about Turnstone goes through here. Two rules it
exists to enforce:

1. The API token never leaves the server. Controllers call this; the browser
   calls controllers.
2. Turnstone is the source of truth. We mirror sessions into Odoo records so
   they are searchable and access-controlled, but we never invent state —
   if Turnstone says a session is idle, it is idle.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
# Creating a session with an initial_message can block while the first turn is
# dispatched, and a tool-heavy turn is slower still.
LONG_TIMEOUT = 120


class TurnstoneError(UserError):
    """A Turnstone API call failed in a way the user should see."""


class TurnstoneClient:
    """Stateless wrapper around one Turnstone server's HTTP API."""

    def __init__(self, base_url, token, timeout=DEFAULT_TIMEOUT):
        if not base_url:
            raise TurnstoneError(_("No Turnstone URL configured."))
        if not token:
            raise TurnstoneError(_("No Turnstone API token configured."))
        self.base_url = base_url.rstrip("/")
        self.api = self.base_url + "/v1/api"
        self.token = token
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(self, method, path, payload=None, params=None, timeout=None):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
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
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:500]
            _logger.warning("Turnstone %s %s -> HTTP %s: %s", method, path, exc.code, body)
            if exc.code in (401, 403):
                raise TurnstoneError(
                    _("Turnstone rejected the API token (HTTP %s). Check the connection's token "
                      "and that it has not been revoked.") % exc.code
                ) from exc
            if exc.code == 404:
                raise TurnstoneError(
                    _("Turnstone has no such resource (HTTP 404 on %s). The session may have been "
                      "deleted on the server.") % path
                ) from exc
            raise TurnstoneError(
                _("Turnstone returned HTTP %(code)s for %(path)s:\n%(body)s")
                % {"code": exc.code, "path": path, "body": body}
            ) from exc
        except urllib.error.URLError as exc:
            raise TurnstoneError(
                _("Cannot reach Turnstone at %(url)s: %(reason)s")
                % {"url": self.base_url, "reason": exc.reason}
            ) from exc
        except json.JSONDecodeError as exc:
            raise TurnstoneError(
                _("Turnstone returned a non-JSON response for %s. Is the URL pointing at the "
                  "Turnstone console?") % path
            ) from exc

    # -- identity ----------------------------------------------------------

    def whoami(self):
        """Who the token belongs to, and what it may do. Used to test a connection."""
        return self._request("GET", "/auth/whoami")

    def health(self):
        """Console health. Lives outside /v1/api, unlike everything else."""
        req = urllib.request.Request(
            self.base_url + "/health",
            headers={"Authorization": "Bearer %s" % self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except Exception as exc:  # noqa: BLE001 - health is best-effort
            raise TurnstoneError(
                _("Cannot reach Turnstone at %(url)s: %(err)s")
                % {"url": self.base_url, "err": exc}
            ) from exc

    # -- catalogue ---------------------------------------------------------

    def skills(self):
        data = self._request("GET", "/skills")
        return data if isinstance(data, list) else data.get("skills", [])

    def personas(self):
        data = self._request("GET", "/admin/personas")
        return data if isinstance(data, list) else data.get("personas", [])

    def models(self):
        return self._request("GET", "/models")

    def schedules(self):
        data = self._request("GET", "/admin/schedules")
        return data if isinstance(data, list) else data.get("schedules", data.get("tasks", []))

    def schedule_runs(self, task_id):
        data = self._request("GET", "/admin/schedules/%s/runs" % task_id)
        return data if isinstance(data, list) else data.get("runs", [])

    # -- sessions ----------------------------------------------------------

    def create_session(self, name=None, skill=None, persona=None, model=None,
                       initial_message=None, kind="interactive"):
        """Start a workstream.

        Deliberately the ROUTE endpoint, not /workstreams/new. The console
        endpoint only ever creates `coordinator` sessions, which delegate work
        to spawned children instead of running tools themselves — fine for
        orchestration, wrong for a chat window. Only the route endpoint accepts
        `kind`.
        """
        payload = {"kind": kind, "client_type": "web"}
        if name:
            payload["name"] = name
        if skill:
            payload["skill"] = skill
        if persona:
            payload["persona"] = persona
        if model:
            payload["model"] = model
        if initial_message:
            payload["initial_message"] = initial_message
        return self._request("POST", "/route/workstreams/new", payload, timeout=LONG_TIMEOUT)

    def session_detail(self, ws_id):
        """Persisted row + live block + message tail, for any session on any node.

        An interactive session lives on whichever node the rendezvous hash
        placed it on, and the console's own /workstreams/{id}/history does NOT
        see it. This cluster-wide endpoint is the one surface that returns both.
        """
        return self._request("GET", "/cluster/ws/%s/detail" % ws_id)

    def send(self, ws_id, message):
        return self._request(
            "POST", "/route/workstreams/%s/send" % ws_id, {"message": message}
        )

    def approve(self, ws_id, approved, feedback=None, cycle_id=None):
        payload = {"approved": bool(approved)}
        if feedback:
            payload["feedback"] = feedback
        if cycle_id:
            payload["cycle_id"] = cycle_id
        return self._request("POST", "/route/workstreams/%s/approve" % ws_id, payload)

    def cancel(self, ws_id):
        return self._request("POST", "/route/workstreams/%s/cancel" % ws_id, {})

    def close(self, ws_id):
        return self._request("POST", "/route/workstreams/%s/close" % ws_id, {})
