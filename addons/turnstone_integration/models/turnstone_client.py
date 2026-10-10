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
import uuid

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
        """Skills, from the ADMIN endpoint rather than /skills.

        The public summary endpoint omits `kind`, and kind is the one field
        that actually matters when starting a session: a coordinator skill run
        as an interactive session has no spawn_* tools, so it cannot delegate —
        and fails by quietly doing the work in one session instead of erroring.
        The controller trims this down to name/description/kind before it
        reaches the browser.
        """
        data = self._request("GET", "/admin/skills")
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
                       initial_message=None, kind="interactive", judge_model=None,
                       auto_approve=None, auto_approve_tools=None):
        """Start a workstream, on whichever surface owns that kind.

        The console/route split applies to CREATION too, and Turnstone is
        explicit about it:

            coordinator workstreams must be created on the console via
            POST /v1/api/workstreams/new (with admin.coordinator scope)

        So coordinators go to the console, which only ever makes coordinators
        and therefore takes no `kind`. Interactive sessions go to the routing
        proxy, which is the only surface that accepts `kind` — the console
        would silently hand back a coordinator instead.

        The console create endpoint takes no `model`, so a coordinator runs on
        the node's default alias. Its children can still be given one, by the
        coordinator itself.
        """
        payload = {}
        if name:
            payload["name"] = name
        if skill:
            payload["skill"] = skill
        if persona:
            payload["persona"] = persona
        if initial_message:
            payload["initial_message"] = initial_message

        if kind == "coordinator":
            # The console create endpoint takes name/skill/persona/initial_message
            # and nothing else — model, judge_model and the auto-approve fields
            # are simply absent from its schema, so passing them would be a lie
            # rather than a setting. Callers are told this at the UI layer.
            return self._request("POST", "/workstreams/new", payload, timeout=LONG_TIMEOUT)

        payload["kind"] = "interactive"
        payload["client_type"] = "web"
        if model:
            payload["model"] = model
        if judge_model:
            payload["judge_model"] = judge_model
        if auto_approve:
            payload["auto_approve"] = True
        if auto_approve_tools:
            payload["auto_approve_tools"] = list(auto_approve_tools)
        return self._request("POST", "/route/workstreams/new", payload, timeout=LONG_TIMEOUT)

    # THE CONSOLE API AND THE NODE API ARE NOT THE SAME SURFACE.
    #
    # A coordinator is hosted by the console, and its send/approve/cancel/close
    # live at /workstreams/{id}/...  An interactive session lives on whichever
    # node the rendezvous hash placed it on, and is reached through the console's
    # routing proxy at /route/workstreams/{id}/...
    #
    # Using the wrong one does not degrade gracefully: the route proxy answers
    # HTTP 404 for a coordinator, and the console answers
    # {"error": "coordinator not found"} for an interactive session. So every
    # per-session call takes the session's kind and picks the prefix.
    @staticmethod
    def _ws_prefix(kind):
        return "/workstreams" if kind == "coordinator" else "/route/workstreams"

    # -- attachments -------------------------------------------------------
    #
    # COORDINATOR-ONLY. Uploading to an interactive session answers
    # {"error": "coordinator not found"}. Verified against 1.8.3, along with
    # which types Turnstone accepts:
    #   image/*            -> kind "image"
    #   application/pdf    -> kind "pdf"
    #   text/*, application/json -> kind "text"
    #   xlsx, docx         -> REJECTED, "Unsupported file type"
    # So Office formats have to be converted before they get here.

    def upload_attachment(self, ws_id, filename, content, mime_type):
        """Multipart upload, built by hand to avoid a requests dependency."""
        boundary = "----turnstone%s" % uuid.uuid4().hex
        body = b"".join([
            ("--%s\r\n" % boundary).encode(),
            ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
             % filename.replace('"', "")).encode(),
            ("Content-Type: %s\r\n\r\n" % mime_type).encode(),
            content,
            ("\r\n--%s--\r\n" % boundary).encode(),
        ])
        url = self.api + "/workstreams/%s/attachments" % ws_id
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer %s" % self.token,
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        })
        try:
            with urllib.request.urlopen(req, timeout=LONG_TIMEOUT) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()[:400]
            try:
                message = json.loads(payload).get("error") or payload
            except json.JSONDecodeError:
                message = payload
            raise TurnstoneError(
                _("Turnstone refused %(name)s: %(err)s") % {"name": filename, "err": message}
            ) from exc
        except urllib.error.URLError as exc:
            raise TurnstoneError(
                _("Cannot reach Turnstone at %(url)s: %(reason)s")
                % {"url": self.base_url, "reason": exc.reason}
            ) from exc

    def list_attachments(self, ws_id):
        data = self._request("GET", "/workstreams/%s/attachments" % ws_id)
        return data.get("attachments", []) if isinstance(data, dict) else (data or [])

    def delete_attachment(self, ws_id, attachment_id):
        return self._request(
            "DELETE", "/workstreams/%s/attachments/%s" % (ws_id, attachment_id)
        )

    def session_detail(self, ws_id):
        """Persisted row + live block + message tail, for any session on any node.

        An interactive session lives on whichever node the rendezvous hash
        placed it on, and the console's own /workstreams/{id}/history does NOT
        see it. This cluster-wide endpoint is the one surface that returns both.
        """
        return self._request("GET", "/cluster/ws/%s/detail" % ws_id)

    def send(self, ws_id, message, kind=None):
        return self._request(
            "POST", "%s/%s/send" % (self._ws_prefix(kind), ws_id), {"message": message}
        )

    def approve(self, ws_id, approved, feedback=None, cycle_id=None, kind=None):
        """Resolve a pending approval.

        A second click on an approval that already landed answers HTTP 409
        "stale cycle_id". That is not a failure — it means the decision was
        recorded and the agent has moved on — so it is reported as a no-op
        rather than raised. Showing the raw 409 tells the user something broke
        when in fact their click worked the first time.
        """
        payload = {"approved": bool(approved)}
        if feedback:
            payload["feedback"] = feedback
        if cycle_id:
            payload["cycle_id"] = cycle_id
        try:
            result = self._request(
                "POST", "%s/%s/approve" % (self._ws_prefix(kind), ws_id), payload
            )
        except TurnstoneError as exc:
            if "stale cycle_id" in str(exc):
                _logger.info(
                    "Approval for %s was already resolved; treating as a no-op", ws_id
                )
                return {"already_resolved": True}
            raise
        return result or {}

    def cancel(self, ws_id, kind=None):
        return self._request("POST", "%s/%s/cancel" % (self._ws_prefix(kind), ws_id), {})

    def cancel_queued(self, ws_id, kind=None):
        """Drop a message queued behind an in-flight turn, without killing the turn."""
        return self._request("DELETE", "%s/%s/send" % (self._ws_prefix(kind), ws_id), {})

    def retry(self, ws_id, kind=None):
        """Re-send the last user message for a fresh answer."""
        return self._request("POST", "%s/%s/retry" % (self._ws_prefix(kind), ws_id), {},
                             timeout=LONG_TIMEOUT)

    def rewind(self, ws_id, turns=1, kind=None):
        """Drop the last N conversation turns."""
        return self._request("POST", "%s/%s/rewind" % (self._ws_prefix(kind), ws_id),
                             {"turns": int(turns)})

    # -- cluster-wide visibility -------------------------------------------

    def cluster_overview(self):
        """Node count, workstream states, aggregate tokens and tool calls."""
        return self._request("GET", "/cluster/overview")

    def cluster_workstreams(self, state=None, search=None, page=1, per_page=100):
        """Every workstream on the cluster, whoever started it.

        This is also how child workstreams are found. Turnstone's
        /workstreams/{id}/children is a COORDINATOR-only endpoint and answers
        "coordinator not found" for an interactive session, but every row here
        carries parent_ws_id, so the parent/child tree can be reconstructed for
        any kind of session.
        """
        params = {"page": page, "per_page": per_page}
        if state:
            params["state"] = state
        if search:
            params["search"] = search
        data = self._request("GET", "/cluster/workstreams", params=params)
        if isinstance(data, list):
            return {"workstreams": data, "total": len(data)}
        return data

    def close(self, ws_id, kind=None):
        return self._request("POST", "%s/%s/close" % (self._ws_prefix(kind), ws_id), {})
