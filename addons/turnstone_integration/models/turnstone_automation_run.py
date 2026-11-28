"""One firing of a rule: did we start a session for this event, and how did it go.

WHY THIS EXISTS

`turnstone.automation` is the trusted configuration — when to ask, what to ask,
who to ask. A run is the *other* half: the fact that a particular event met a
particular rule at a particular moment. Without a row per attempt there is no
answer to "did the ticket that arrived at 09:14 ever reach the agent", and no
place to put the answer "no, because".

Three properties matter more than the field list, and each one is a decision:

**A run is keyed, not counted.** `idempotency_key` identifies the *event* — a
webhook delivery id, a message id, a record write — and is unique per rule.
A retried webhook, a cron that overlaps itself, a server action that runs twice
because the transaction was retried: all of them arrive with the same key, and
the second arrival is answered with the first run instead of starting a second
session. Turnstone's create endpoints have no idempotency of their own, so this
is the only thing standing between a retry and a duplicate transcript. Because
the key is what makes that true, it cannot be edited afterwards
(`IMMUTABLE_FIELDS`): a run that could be re-keyed could be made to answer for
another event, and the unique constraint would mean nothing.

**State moves along a graph, through methods.** `queued → running → done |
failed`, plus `queued → skipped` for the decisions taken before anything is
attempted, and `failed → running` for a retry. `write()` refuses any other move
and the `_mark_*` methods are the only places that move a run in normal code, so
"how did this get to done without ever starting" is not a question anyone has to
ask. `done` and `skipped` are terminal: a new event is a new run, not a reopened
old one — otherwise the history stops being a history.

**The prompt is not kept unless someone asked for it.** A rendered prompt is
built from business records: a customer's name, an address, the body of a
message. Storing it on every run would put that data in a second place by
default, so `turnstone.automation.store_prompt` decides, and this model enforces
the decision in both directions — `_record_prompt()` is a no-op when the rule
does not keep prompts, and a direct write of `prompt` on such a rule raises
rather than silently retaining customer data (silently ignoring the write would
be worse: the caller would believe it was stored).

WHAT A STATE MEANS — AND WHAT IT DOES NOT

The states describe the *launch*, not the agent's work. `done` means "a
Turnstone session exists and the prompt was delivered to it"; from there the
session has its own `state` (`thinking`, `attention`, `closed`, …) and its own
transcript. This module does not poll headless sessions, so a run cannot
honestly claim the agent finished — and a run whose session later fails stays
`done`, because the launch did succeed. `error` therefore answers "why did this
event not reach an agent", which is the question a person actually asks.
"""

import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .redaction import redact

# The state graph. `done` and `skipped` are terminal by having no successors: a
# later event is a new run with its own key, never a reopened row — a history
# that can be rewritten is not a history.
ALLOWED_TRANSITIONS = {
    "queued": ("running", "failed", "skipped"),
    "running": ("done", "failed"),
    "failed": ("running",),
    "done": (),
    "skipped": (),
}

# States that count as "this event is already being handled" for a rule's
# dedupe window. A failed run is deliberately absent: it did not handle the
# event, so suppressing the next one would drop the event entirely. A skipped
# run is absent for the same reason — it is a decision *not* to handle it.
WINDOW_STATES = ("queued", "running", "done")

# The fields that give a run its identity. Frozen after create: see the module
# docstring on why a re-keyable run makes the unique constraint worthless.
IMMUTABLE_FIELDS = (
    "automation_id",
    "idempotency_key",
    "dedupe_token",
    "trigger_res_model",
    "trigger_res_id",
)

# An error message is for a person reading a run history, not a stack dump. It
# is capped so a 40-line HTML error page cannot become the record, and cut with
# a visible marker so nobody mistakes a truncated message for the whole one.
MAX_ERROR_CHARS = 2000

# What a credential looks like when a client library prints a request lives in
# `models/redaction.py`, shared with `TurnstoneClient` — the client's own warning
# line is written before any run exists, and two copies of these patterns would
# let one drift and re-open the leak the other closed.


class TurnstoneAutomationRun(models.Model):
    _name = "turnstone.automation.run"
    _description = "Turnstone Automation Run"
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    automation_id = fields.Many2one(
        "turnstone.automation", string="Automation", required=True,
        ondelete="cascade", index=True,
        help="The rule this event met. A run is only meaningful against the rule "
             "that produced it — which agent, which prompt — so it goes when the "
             "rule does.",
    )
    company_id = fields.Many2one(
        "res.company", related="automation_id.company_id", store=True,
        index=True, readonly=True,
        help="The rule's company, carried here so a run history can be scoped "
             "the same way the rule is.",
    )

    # -- the event ----------------------------------------------------------
    idempotency_key = fields.Char(
        required=True, index=True, copy=False, readonly=True,
        help="Identifies the event that fired the rule: a webhook delivery id, a "
             "message id, a record write. Unique per automation, so a retried "
             "delivery finds the run it already produced instead of starting a "
             "second session. Callers with no natural event id use "
             "`_generate_key()`.",
    )
    dedupe_token = fields.Char(
        index=True, copy=False, readonly=True,
        help="Identifies what the run is *about*, coarser than the idempotency "
             "key — usually the record, so two writes to one ticket share a token "
             "while being two distinct events. This is what the rule's "
             "'Suppress Repeats' window looks up; without it the window cannot "
             "match anything.",
    )
    trigger_res_model = fields.Char(
        string="Trigger Model", index=True, readonly=True,
        help="Model of the record that fired the rule. Kept as plain text because "
             "the model may be uninstalled later and the run must still say what "
             "it was about.",
    )
    trigger_res_id = fields.Integer(string="Trigger Record ID", readonly=True)

    # -- the launch ---------------------------------------------------------
    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        required=True, default="queued", readonly=True, index=True,
        help="Where the launch got to, not where the agent got to: 'Done' means "
             "a session exists and the prompt was delivered. The session's own "
             "state tracks the work from there.",
    )
    attempt_count = fields.Integer(
        readonly=True, default=0, copy=False,
        help="How many times a launch was attempted. 0 with a failed run means it "
             "was refused before anything was tried — a permission or "
             "configuration problem, not a transport one.",
    )
    session_id = fields.Many2one(
        "turnstone.session", string="Session", ondelete="set null", index=True,
        readonly=True,
        help="The session this run started. Set when the launch succeeds; a failed "
             "run has none, because the workstream is created by the same call.",
    )
    error = fields.Text(
        readonly=True, copy=False,
        help="Why the event did not reach an agent. Also carries the reason a run "
             "was skipped: a decision with no reason is not reviewable. Bounded "
             "and stripped of credentials.",
    )
    prompt = fields.Text(
        readonly=True, copy=False,
        help="The rendered prompt — only kept when the rule asks for it. It is "
             "built from business records, so retaining it is a decision "
             "(turnstone.automation.store_prompt), not a default.",
    )

    # -- timestamps ---------------------------------------------------------
    queued_at = fields.Datetime(
        readonly=True, default=fields.Datetime.now, copy=False,
        help="When the event arrived.",
    )
    started_at = fields.Datetime(
        readonly=True, copy=False,
        help="When the first launch attempt began. A retry does not move it: this "
             "is when the run started trying, not when it last tried.",
    )
    finished_at = fields.Datetime(
        readonly=True, copy=False,
        help="When the run reached a terminal state. A retried failure is stamped "
             "again when the retry ends.",
    )

    _run_key_uniq = models.Constraint(
        "unique(automation_id, idempotency_key)",
        "This event already has a run for that automation.",
    )

    @api.depends("automation_id.name", "trigger_res_model", "trigger_res_id")
    def _compute_name(self):
        """A run reads as "rule: what it was about", for lists and chatter.

        Only trusted configuration and identifiers go in the name — never the
        prompt, which is untrusted content built from business records.
        """
        for rec in self:
            rule = rec.automation_id.name or _("Automation")
            if rec.trigger_res_id:
                rec.name = "%s: %s,%s" % (
                    rule, rec.trigger_res_model or _("record"), rec.trigger_res_id,
                )
            else:
                rec.name = rule

    # -- creation and writes are where the invariants live ------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            state = vals.get("state", "queued")
            if state != "queued":
                raise UserError(_(
                    "A run starts queued. Let the launch move it to %(state)s "
                    "instead of creating it there — a row that claims to have "
                    "started has to have something to show for it.",
                ) % {"state": state})
            if vals.get("error"):
                vals["error"] = self._clean_text(vals["error"])
        # Retention is checked on the created rows rather than on the incoming
        # values: the rule is reached by id, and by the time the rows exist the
        # relation is resolved. Raising here rolls the whole create back, so a
        # refused prompt leaves no half-written run behind.
        records = super().create(vals_list)
        records.filtered("prompt")._check_prompt_retention_and_raise()
        return records

    def write(self, vals):
        vals = dict(vals)
        if "state" in vals:
            self._check_transition(vals["state"])
        frozen = [name for name in IMMUTABLE_FIELDS if name in vals]
        if frozen:
            raise UserError(_(
                "A run's %(fields)s cannot be changed after it is created: they "
                "are what make its idempotency key mean one event and no other. "
                "If the event is genuinely a different one, create a run for it.",
            ) % {"fields": ", ".join(sorted(frozen))})
        if vals.get("prompt"):
            self._check_prompt_retention_and_raise()
        if vals.get("error"):
            vals["error"] = self._clean_text(vals["error"])
        return super().write(vals)

    def _check_transition(self, new_state):
        """Refuse a move the graph does not allow, naming the states available."""
        for rec in self:
            # Writing the state it already has is a no-op, as it is for any other
            # field in Odoo: a form save that re-sends the state must not be told
            # its record is final.
            if new_state == rec.state:
                continue
            labels = dict(rec._fields["state"]._description_selection(self.env))
            allowed = ALLOWED_TRANSITIONS.get(rec.state, ())
            if new_state in allowed:
                continue
            if not allowed:
                raise UserError(_(
                    "Run %(run)s is %(from)s, which is final. A later event is a "
                    "new run, not a reopened one — create a run for it.",
                ) % {"run": rec.display_name, "from": labels.get(rec.state, rec.state)})
            raise UserError(_(
                "Run %(run)s cannot go from %(from)s to %(to)s. From %(from)s the "
                "only moves are: %(allowed)s.",
            ) % {
                "run": rec.display_name,
                "from": labels.get(rec.state, rec.state),
                "to": labels.get(new_state, new_state),
                "allowed": ", ".join(labels.get(state, state) for state in allowed),
            })

    # -- the transitions, one method each -----------------------------------

    def _transition(self, new_state, **vals):
        """Move this run to `new_state`, with the values that explain the move.

        A call that finds the run already in `new_state` writes nothing and
        returns. That is what makes the `_mark_*` methods safe to call twice — a
        launch that retries its bookkeeping, or a job re-run for the same event —
        without either failing the launch or inflating `attempt_count` and
        restamping `finished_at`, which would turn two calls into a history that
        says something that did not happen. Real progress is still a real
        transition: after a failure the state is `failed`, so the next attempt
        does move it and does count.
        """
        self.ensure_one()
        if self.state == new_state:
            return self
        self.write(dict(vals, state=new_state))
        return self

    def _mark_running(self):
        """Someone is about to try. Counts the attempt and stamps the first one.

        Called before the remote call, so a crash between here and the answer
        leaves a run in `running` — which is honest (an attempt began and its
        fate is unknown) and visible, unlike a row still saying `queued`.
        """
        self.ensure_one()
        return self._transition(
            "running",
            attempt_count=(self.attempt_count or 0) + 1,
            started_at=self.started_at or fields.Datetime.now(),
        )

    def _mark_done(self, session=None):
        """The prompt was delivered. `session` is the session it was delivered to."""
        self.ensure_one()
        vals = {"finished_at": fields.Datetime.now()}
        if session is not None:
            vals["session_id"] = session.id
        return self._transition("done", **vals)

    def _mark_failed(self, error, secrets=None):
        """The attempt failed. `secrets` are redacted out of `error` on the way in."""
        self.ensure_one()
        return self._transition(
            "failed",
            error=self._clean_text(error, secrets),
            finished_at=fields.Datetime.now(),
        )

    def _mark_skipped(self, reason=None, secrets=None):
        """Nothing was attempted, and why. `secrets` are redacted out of `reason`."""
        self.ensure_one()
        return self._transition(
            "skipped",
            error=self._clean_text(reason, secrets) if reason else False,
            finished_at=fields.Datetime.now(),
        )

    # -- prompt retention ---------------------------------------------------

    def _check_prompt_retention_and_raise(self):
        """Refuse to keep a prompt no rule asked us to keep.

        Raising is deliberate. Dropping the value quietly would be the friendlier
        failure and the worse one: the caller would go on believing the prompt it
        sent is on the run, and the answer to "what exactly did the agent receive"
        would be a silence nobody notices until they need it.
        """
        for rec in self:
            if not rec.automation_id.store_prompt:
                raise UserError(_(
                    "%(rule)s does not keep rendered prompts, so this run may not "
                    "store one. Turn on \"Keep the Rendered Prompt\" on the rule "
                    "while investigating, then turn it back off — the prompt is "
                    "built from business records, which are not retained here by "
                    "default.",
                ) % {"rule": rec.automation_id.name or _("this automation")})

    def _record_prompt(self, body):
        """Keep the rendered prompt — but only if the rule asks for it.

        Returns `True` when it was stored and `False` when retention is off. A
        caller does not have to check the rule first: sending the prompt is
        always allowed, keeping it is the rule's decision.
        """
        self.ensure_one()
        if not body or not self.automation_id.store_prompt:
            return False
        self.write({"prompt": body})
        return True

    def _forget_prompts(self):
        """Drop the stored prompt from these runs.

        The retention flag governs what may be written; turning it back off does
        not reach into runs that were written while it was on, so a rule that was
        used for debugging needs a way to take the data back out. Nothing calls
        this automatically yet — the rule's form is where it belongs.
        """
        kept = self.filtered("prompt")
        if kept:
            kept.sudo().write({"prompt": False})
        return len(kept)

    # -- helpers the launch service and the adapters call -------------------

    @api.model
    def _generate_key(self):
        """An idempotency key for a caller that has no natural event id.

        Deliberately not derived from record content: a key made of field values
        would collide for two genuinely different events that happen to look
        alike, and the second one would be swallowed as a duplicate.
        """
        return uuid.uuid4().hex

    @api.model
    def _find_by_key(self, automation, idempotency_key):
        """The run this event already produced, or an empty recordset.

        Searched under `sudo()`: whether the event already has a run is
        bookkeeping, and a record rule must not make a caller create a second
        one that then trips the unique constraint. The recordset returned is the
        caller's own, so reading its fields still applies their rules.
        """
        if not automation or not idempotency_key:
            return self.browse()
        found = self.sudo().search([
            ("automation_id", "=", automation.id),
            ("idempotency_key", "=", idempotency_key),
        ], limit=1)
        return self.browse(found.ids)

    @api.model
    def _open_for_event(self, automation, idempotency_key, *, record=None,
                        dedupe_token=None):
        """The run for this event, opening one if there is none. ``(run, created)``.

        `_find_by_key()` followed by `create()` has a window two concurrent
        callers can both walk through: a webhook delivered twice at once, or a
        cron overlapping itself, both find nothing and both create. The unique
        constraint catches it — but as an `IntegrityError`, which would fail one
        of the two callers instead of telling it that the event is already
        handled. So the insert is attempted inside a savepoint and, when the
        constraint refuses it, the row the other caller wrote is returned with
        ``created=False``. That caller owns the launch; this one has nothing to
        do.

        ``created=False`` is therefore not an error and not a duplicate run: it
        is the answer "someone else got here first". The caller must only launch
        when ``created`` is True.
        """
        existing = self._find_by_key(automation, idempotency_key)
        if existing:
            return existing, False
        vals = {
            "automation_id": automation.id,
            "idempotency_key": idempotency_key,
        }
        if dedupe_token:
            vals["dedupe_token"] = dedupe_token
        if record:
            # Plain text, not a reference: the model may be uninstalled later and
            # the run still has to say what it was about.
            vals["trigger_res_model"] = record._name
            vals["trigger_res_id"] = record.id
        try:
            with self.env.cr.savepoint():
                return self.sudo().create(vals), True
        except Exception:  # noqa: BLE001 - the constraint is not the only writer
            # Re-read rather than guess: if the row is there now, this insert lost
            # a race and the other transaction's run is the one to use. If it is
            # not, the failure was something else (a missing column, a bad value)
            # and re-raising it is the honest answer.
            raced = self._find_by_key(automation, idempotency_key)
            if raced:
                return raced, False
            raise

    @api.model
    def _lock_for_retry(self, run):
        """The run if this caller may retry it, or an empty recordset.

        A retry is the one place the unique constraint cannot help: the row
        already exists, so two callers retrying the same key would both find it
        `failed`, both move it to `running` and both start a session — and the
        key would then mean two workstreams, which is the whole thing it exists
        to prevent. So the retry claims the row under a database lock and re-reads
        its state from the database, not from this transaction's cache: whoever
        loses sees a run that is no longer `failed` and returns it untouched.

        Used only on the retry path, because only there does the run pre-exist
        with a state that permits another attempt.
        """
        if not run:
            return self.browse()
        self.env.cr.execute(
            "SELECT id FROM turnstone_automation_run WHERE id = %s FOR UPDATE",
            [run.id],
        )
        if not self.env.cr.fetchone():
            return self.browse()
        locked = self.sudo().browse(run.id)
        # The lock says the row is ours; the cache may still hold the state it
        # had before the other transaction committed, and only the database's
        # answer decides whether an attempt is still allowed.
        locked.invalidate_recordset()
        return locked if locked.state == "failed" else self.browse()

    @api.model
    def _window_duplicate(self, automation, dedupe_token):
        """A run of `automation` about this token that is still inside the window.

        This is the other half of `turnstone.automation._dedupe_since()`: the
        rule says how far back to look, this says what was found. Only runs that
        actually handle the event count (`WINDOW_STATES`) — a failure or a skip
        did not handle it, so suppressing the next event would lose it.

        Raises when the rule has a window configured but the caller has no token:
        a window that can never match is a setting that quietly does nothing,
        and the caller is the only one who knows what "the same thing" means.
        """
        since = automation._dedupe_since() if automation else None
        if not since:
            return self.browse()
        if not dedupe_token:
            raise UserError(_(
                "%(rule)s suppresses repeats within %(minutes)s minutes, but this "
                "run has no dedupe token — so the window could never match and "
                "every event would fire. Give the run the token that identifies "
                "what it is about (usually the record).",
            ) % {
                "rule": automation.name,
                "minutes": automation.dedupe_window_minutes,
            })
        found = self.sudo().search([
            ("automation_id", "=", automation.id),
            ("dedupe_token", "=", dedupe_token),
            ("create_date", ">=", since),
            ("state", "in", list(WINDOW_STATES)),
        ], order="id desc", limit=1)
        return self.browse(found.ids)

    # -- text hygiene -------------------------------------------------------

    @api.model
    def _clean_text(self, text, secrets=None):
        """A message safe to keep: bounded, and with credentials taken out.

        `secrets` is what the caller knows is secret (an API token it just used);
        the patterns catch the shapes a client library prints on its own. A run
        history is readable by more people than a connection is, so an error
        message is exactly the wrong place for a bearer header.
        """
        text = redact(text, secrets)
        if len(text) > MAX_ERROR_CHARS:
            text = text[:MAX_ERROR_CHARS] + _(" … [truncated]")
        return text
