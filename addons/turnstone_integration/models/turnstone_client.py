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

# How many conversation rows a session detail read asks for.
#
# `GET /cluster/ws/{id}/detail` takes exactly one query parameter,
# `message_limit` -- "Max conversation rows in the tail (default 20, clamped to
# 200)" -- and two things about it are easy to miss, both of which cost real
# history:
#
#   * the default is 20, and the rows are the TAIL: the last 20 turns, not the
#     first. A client that sends nothing gets a truncated conversation;
#   * the response carries no `total`, `has_more` or `offset`, so a caller that
#     receives 20 rows cannot tell a tail from a whole transcript.
#
# Sending nothing is therefore not a neutral default. On a real Turnstone 1.8.3,
# 82 of 107 workstreams answered at exactly 20 rows with no limit sent, and all
# 82 were truncated when asked for 500 -- 7,698 turns never served (OTI-5763).
# 200 is the server's hard clamp: asking for more does not help. It is still a
# tail for the sessions that exceed 200 rows, but that is for the sync guard to
# disclose (OTI-5772), not something this call can fix. Do not put this
# parameter on any other endpoint: none of them accept it.
SESSION_DETAIL_MESSAGE_LIMIT = 200


class TurnstoneError(UserError):
    """A Turnstone API call failed in a way the user should see."""


class TurnstoneConflict(TurnstoneError):
    """Turnstone answered HTTP 409: the resource is already there.

    A subclass so every existing ``except TurnstoneError`` still catches it.
    It exists because 409 means something different from every other failure
    for a create: the caller is not being refused, it is being told that the
    thing it asked for already exists -- which, when the caller supplied the
    id itself, is the "this already happened" answer a retried request needs
    (``create_session``'s ``ws_id``). The message text is useless for that
    decision -- it is translated.
    """


class TurnstoneNotFound(TurnstoneError):
    """Turnstone answered HTTP 404: the resource is not there any more.

    A subclass so every existing ``except TurnstoneError`` still catches it.
    It exists because 404 means something different from every other failure
    for a session: the workstream is gone, rather than unreachable, and only a
    caller that can tell the two apart may act on the difference. The same
    message text is useless for that decision -- it is translated.
    """


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
                raise TurnstoneNotFound(
                    _("Turnstone has no such resource (HTTP 404 on %s). The session may have been "
                      "deleted on the server.") % path
                ) from exc
            if exc.code == 409:
                raise TurnstoneConflict(
                    _("Turnstone already has this resource (HTTP 409 on %s): %s")
                    % (path, body)
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
                       auto_approve=None, auto_approve_tools=None, ws_id=None):
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
            #
            # `ws_id` is absent from that schema too: the console has no way to
            # be told which id to use, so a coordinator create cannot be made
            # idempotent this way. Dropping the argument silently is right --
            # the alternative is a create that looks keyed and is not -- and
            # the caller is told (see `TurnstoneSession._ensure_started`).
            return self._request("POST", "/workstreams/new", payload, timeout=LONG_TIMEOUT)

        if ws_id:
            # A caller-supplied id is honoured by the routing proxy verbatim,
            # and a SECOND create with the same one answers HTTP 409
            # "Workstream already exists" instead of making a second
            # workstream. That is the native idempotency mechanism this module
            # uses to survive a transaction retry re-running a create; it is
            # not documented as an idempotency key, but it behaves as one for
            # JSON callers because the destination id is reserved before the
            # body lands. Verified against Turnstone 1.8.3 (OTI-5763 Q3).
            payload["ws_id"] = ws_id

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

        Always asks for the whole tail the server will give
        (``SESSION_DETAIL_MESSAGE_LIMIT``). Leaving the parameter off is not a
        neutral default: the server answers with the last 20 turns, says nothing
        about having done so, and a mirror fed from that response silently
        drops the oldest turns of every conversation longer than 20 (OTI-5763).
        """
        return self._request(
            "GET",
            "/cluster/ws/%s/detail" % ws_id,
            params={"message_limit": SESSION_DETAIL_MESSAGE_LIMIT},
        )

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
