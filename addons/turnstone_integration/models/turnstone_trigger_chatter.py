"""The chatter adapter: a customer wrote on a record, so a rule may want to run.

WHY THIS EXISTS

`turnstone.trigger.record` (#5752) notices a business record being created or
changed. It says nothing about a **message** arriving on that record, which is
the other half of "something happened that an agent should answer": a customer
replies in the chatter of a ticket, a portal user comments on an order, an
inbound email lands on a lead. `turnstone.automation` already reserves the
trigger value for it (`chatter_message`, "Message received on a record"), and so
does `TARGET_MODEL_REQUIRED_FOR`, because a chatter rule needs a target model to
render its prompt from. This file is what turns that reserved value into a
firing.

THE FAILURE MODE IS A FEEDBACK LOOP, AND THIS IS DESIGNED AROUND IT

The agent answers in the same chatter it was woken by. Its answer is itself a
`mail.message` on the same record, so a naive "any message on a matching record"
trigger would fire the rule again, and the agent would answer its own answer
forever. Everything below exists to make that impossible by construction rather
than by a filter somebody has to remember to configure:

* **Who authored it decides.** The rule is Odoo's own, lifted from
  `base_automation.make_message_post()`: a message is *inbound* when it has no
  author (it came from outside — the mail gateway posting for an address that
  matched no partner) or when its author is a **share** partner
  (`res.partner.partner_share`, i.e. a portal customer: a partner with no
  internal user behind it). An internal user's message is *outbound* and never
  fires this adapter. An agent writing back through the Odoo MCP server is an
  internal user, so its reply is outbound by that rule alone — the loop cannot
  close.
* **Two explicit identity checks on top of that**, for a deployment that gives
  its automation a portal identity instead of an internal one: a message is
  refused when it is authored by the rule's `run_as_user_id` partner or by the
  configured automation user's partner. Belt and braces, because the cost of the
  loop closing is unbounded and the cost of this check is one comparison.
* **Internal notes and system notifications are not customer messages.**
  `is_internal`, an internal subtype (`mail.mt_note` — which is what both the
  chatter composer's "Log note" and the gateway's reply-to-a-note path select),
  and the `message_type` values Odoo itself skips are all excluded, plus
  `email_outgoing` and `out_of_office`, which are never a customer writing
  something.
* **A message on a record no rule watches costs nothing.** The same frozenset
  gate #5752 uses, on the message's *target* model, before any query.

WHAT HAPPENS WHEN ONE DOES FIRE: ENQUEUE, NEVER LAUNCH

Exactly as in #5752, and for the same reason: this hook runs inside the poster's
transaction, and the launch service *is* an HTTP call. So the adapter's only
write is a `turnstone.automation.run` row in state `queued`, which #5754's
worker drains. It reuses `turnstone.trigger.record._enqueue()` rather than
reimplementing queueing, so the per-transaction bound (`MAX_RUNS_PER_TRANSACTION`),
the company wall and the rule's record domain are one implementation, and a
chatter run and a record run draw on the same budget within one transaction.

THE MESSAGE ID IS THE IDENTITY

The run's `idempotency_key` is `chatter:<mail.message id>`. That is the whole of
the duplicate-delivery answer: a message row is created once, and
`unique(automation_id, idempotency_key)` means a second delivery of the *same*
message (a retried transaction, a replayed gateway delivery, a hook that fires
twice) finds the first run and queues nothing instead of starting a second
session. It is also why the key is not derived from the body: two genuinely
different messages can say the same thing.

The **trigger record** on the run is the business record the message is attached
to (`message.model` / `message.res_id`), *not* the message. Two reasons, both
load-bearing:

1. `turnstone.automation._render_prompt(record)` refuses a record whose model is
   not the rule's `target_model`. A run pointing at `mail.message` would fail to
   render its prompt on every launch and land `failed` — a rule that looks
   broken for the most ordinary reason in the world.
2. #5754's drainer rebuilds the record from `trigger_res_model` /
   `trigger_res_id` to compose the prompt. The thing that can answer "what does
   the agent need to know" is the record; the *message* is the event.

The `dedupe_token`, by contrast, is the record (`<model>,<id>`) — which is
`_enqueue()`'s own default and what the rule's "Suppress Repeats" window
searches. For a conversation on one ticket, "this thing" is the ticket.

THE PROMPT: THE INSTRUCTION, THEN THE MESSAGE, FENCED

`prompt_for_run()` composes what the agent receives: the administrator's
rendered template as the instruction, then a fenced block carrying the message's
author, date and body — and **never the rest of the thread**, which is what the
ticket asks for and also what keeps a long conversation from spending the
agent's whole context on history it can fetch itself.

The body is third-party text. It is fenced after the instruction, with the fence
made longer than any backtick run inside it (CommonMark closes a fence only on a
run at least as long), so a message cannot end the block early and put its own
words back at instruction level. Same shape and same reasoning as
`_webhook_prompt()`.

Composition is separate from queueing on purpose. The run model **raises** on a
stored prompt when the rule does not keep prompts
(`_check_prompt_retention_and_raise`), and "Keep the Rendered Prompt" is off by
default. So:

* this adapter stores the composed prompt through `run._record_prompt()`, which
  is a no-op unless the rule opted in;
* **the drainer calls `prompt_for_run(run)` to rebuild it at launch time.** That
  is the seam #5754 must use — `rule._render_prompt(record)` alone would reach
  the agent with an instruction and no message to act on.

Composing it twice is what makes retention a retention decision rather than a
functional one: with "Keep the Rendered Prompt" off, the agent still gets the
customer's words, they are simply not written down a second time.

WHY THE HOOK IS ON `BaseModel.create` AND NOT `mail.message.create`

`base_automation._unregister_hook()` deletes `create`, `write`,
`_compute_field_value`, `unlink`, `_onchange_methods` **and `message_post`** from
*every* class in `registry.values()`, and re-adds only its own. A layer installed
on `mail.message.create` — or on `mail.thread.message_post` — is therefore
removed the first time an administrator edits any base automation, and the
adapter silently stops firing. `BaseModel` is not a registered model class, so
this layer cannot be taken out from underneath us; it is installed at import
time, before any `_register_hook()`, so it sits at the bottom of the chain and a
later patch wraps it as its `origin`.

The ordering against #5752's own `BaseModel.create`/`write` layer is then the
only subtlety, and it is handled explicitly rather than left to import order:
`_install_base_patch()` reads `.origin` of whatever it finds instead of assuming
`BaseModel`, so whichever module is imported second wraps the first and both
dispatch. The two dispatch on disjoint models — #5752 on every model a record
rule watches, this one only on `mail.message` and on the rule model's own
watch-list fields — so the order they run in does not matter.

`mail.message.create` is the single funnel every origin goes through: the chatter
composer, `message_post()` (which ends in `_message_create()` →
`mail.message.create`), the mail gateway (`_message_route_process()` →
`message_post()`), a direct `create()` from another module, and `mail.mail`'s
`_inherits` delegation. One layer catches all of them.

THE WATCHED SET, AND WHY THIS FILE ALSO PATCHES `write`

`_refresh_watch_list()` stores the models some active `chatter_message` rule
watches, on the registry (per process, per database, rebuilt on every registry
setup). It runs from `_register_hook()`, from `_unregister_hook()` (which only
empties the set — removing the patch would take every layer above it too), and
whenever the **rule model's own** row is created or a write touches `active`,
`trigger_type` or `target_model_id` — the fields that can change the set.

That is why there is a `write` layer as well as a `create` one: without it a
rule switched from `record_create` to `chatter_message` would not start firing
until the next registry reload, which is a "I configured it and nothing happened"
bug. Both layers return on a single `self._name` comparison for every other
model, so the cost on the database's hot path is one string compare.

WHAT IS STILL NOT COVERED

A message written to the table through raw SQL fires nothing. That is true of
every ORM hook in the system, including `base_automation`'s, and is not worth a
repeated-scan cron.
"""

import logging
import re

from odoo import api, fields, models
from odoo.models import BaseModel
from odoo.tools import html2plaintext
from odoo.tools.sql import table_exists

_logger = logging.getLogger(__name__)

# The one trigger value this adapter answers. Defined here rather than imported
# from #5752, because importing a *constant* across modules would make either
# file's import order matter for a value that never changes.
CHATTER_TRIGGERS = ("chatter_message",)

# The rule model, and the fields of it that can change which models this adapter
# watches.
RULE_MODEL = "turnstone.automation"
WATCH_LIST_FIELDS = frozenset({"active", "trigger_type", "target_model_id"})

# The model every message is a row of — the only model the message layer
# dispatches on.
MESSAGE_MODEL = "mail.message"

# The attribute the watched-model set lives under on the registry. A *different*
# attribute from #5752's, because it is a different question: #5752 asks "does a
# record rule watch this model", this one asks "does a chatter rule watch it".
_WATCH_ATTR = "turnstone_chatter_adapter_models"

# The re-entrancy guard, in `cr.cache` rather than in `context`: nothing a
# caller does (`with_context`, a nested savepoint) can leak or lose it, and the
# transaction's end takes it away. The same decision #5752 documents.
_DISPATCHING_KEY = "turnstone_chatter_adapter.dispatching"

# Marks the functions this module installs, so a second install is detected
# rather than wrapped twice. `BaseModel` is shared by every registry in this
# process, so wrapping it twice would dispatch every message twice.
_PATCH_MARKER = "_turnstone_chatter_adapter_patch"
_PATCH_INSTALLED = False

# `message_type` values that are never a customer writing something. The first
# three are the ones Odoo's own `make_message_post()` skips; `email_outgoing` is
# a mailing this instance sent and `out_of_office` is an automatic reply —
# neither is somebody asking the agent anything. That leaves `comment` (what the
# chatter composer and the website/portal form post) and `email` (incoming mail)
# as the two a customer's message actually carries.
NOT_INBOUND_TYPES = frozenset({
    "notification",       # system notification: a tracked change, a creation log
    "auto_comment",       # automated targeted notification
    "user_notification",  # generated for one recipient, via message_notify
    "email_outgoing",     # a mailing this instance sent
    "out_of_office",      # an automatic reply
})

# How much of one message body reaches the prompt. A body is text a customer
# typed; one message must not be able to spend the agent's whole context, and a
# visible truncation marker is cheaper to explain than a silent cut.
MAX_BODY_CHARS = 20_000

# The `ir.config_parameter` the installation-wide automation identity lives in.
# Read, never written, here: it is #5747's setting.
AUTOMATION_USER_PARAM = "turnstone_integration.automation_user_id"

# A run of backticks, so a fence can be made longer than anything inside it.
_BACKTICKS_RE = re.compile(r"`+")


def _watched_models(registry):
    """The models some active chatter rule watches, for this registry."""
    return getattr(registry, _WATCH_ATTR, frozenset())


def _dispatch_messages(env, messages):
    """Hand one batch of created messages to the adapter, swallowing all errors.

    The hot path is one set lookup per created message on the *target* model,
    before any query or `sudo()`: a message on a model no chatter rule watches
    costs a frozenset test and nothing else.

    The outermost guard is here because this runs inside somebody's
    `message_post()`. A broken rule, an unreadable field, a domain naming a field
    another module renamed — none of it may turn a customer's reply into a
    traceback in front of them.
    """
    watched = _watched_models(env.registry)
    if not watched:
        return
    interesting = env[MESSAGE_MODEL]
    for message in messages:
        # A message with no document is a Discuss channel message, an activity
        # notification, or another threadless post: no record for a rule to
        # watch and nothing to render a prompt from.
        if message.model and message.res_id and message.model in watched:
            interesting |= message
    if not interesting:
        return

    cache = env.cr.cache
    if cache.get(_DISPATCHING_KEY):
        # A message created by this adapter's own work — a notification logged
        # by a failed queue write, say. A consequence of the event being handled,
        # not a new event.
        return
    cache[_DISPATCHING_KEY] = True
    try:
        env["turnstone.trigger.chatter"]._on_messages(interesting)
    except Exception:  # noqa: BLE001 - see the docstring above
        _logger.exception(
            "Turnstone chatter adapter failed on %s message(s); they were posted "
            "normally.", len(interesting),
        )
    finally:
        cache.pop(_DISPATCHING_KEY, None)


def _dispatch_rule_change(env, vals):
    """Refresh the watch list when a rule save could have changed it.

    Only the fields that decide the set are considered. `_note_run()` bumps
    `run_count`/`last_run_at` on every launch, so refreshing on *any* rule write
    would re-search the rules table on every launch for nothing — the same
    reasoning #5752 documents.

    Its own guard: a rule save must not fail because the adapter's bookkeeping
    could not be recomputed, and the next message will find the set either way.
    """
    if vals is not None and not WATCH_LIST_FIELDS.intersection(vals):
        return
    try:
        env["turnstone.trigger.chatter"]._refresh_watch_list()
    except Exception:  # noqa: BLE001 - a rule save may not fail on a cache refresh
        _logger.exception(
            "Turnstone chatter adapter could not refresh its watch list after a "
            "rule save; the rule was saved normally.",
        )


def _install_base_patch():
    """Install the `BaseModel.create`/`write` layers this adapter needs.

    Called at import time, before any module's `_register_hook()`, for the reason
    in the module docstring: `base_automation._unregister_hook()` deletes
    `create`/`write` from every registered model *class*, so a layer on
    `mail.message` would not survive the next automation edit, while `BaseModel`
    is not one of those classes.

    `.origin` is read from whatever is already installed rather than assumed to
    be `BaseModel.create`/`write`: #5752 installs layers of its own on the same
    two names from this same package, and the two must chain rather than replace
    each other regardless of which one Odoo happens to import first.

    Idempotent: a second call (a test may make one) sees the marker and returns.
    """
    global _PATCH_INSTALLED  # noqa: PLW0603 - one patch per process, on purpose
    if _PATCH_INSTALLED or getattr(BaseModel.create, _PATCH_MARKER, False):
        _PATCH_INSTALLED = True
        return

    base_create, base_write = BaseModel.create, BaseModel.write

    @api.model_create_multi
    def create(self, vals_list, **kw):
        records = create.origin(self, vals_list, **kw)
        name = records._name
        if name == MESSAGE_MODEL:
            _dispatch_messages(self.env, records)
        elif name == RULE_MODEL:
            # A new rule can add a model to the set, and a rule created as
            # `chatter_message` on a model this process has never seen must fire
            # on the very next message.
            _dispatch_rule_change(self.env, None)
        return records

    def write(self, vals, **kw):
        # The name is checked *before* anything else: this layer is on every
        # write in the database, so an unrelated model must cost one string
        # compare and no `set()` allocation. Only the rule model needs the
        # watch list refreshed.
        if self._name != RULE_MODEL:
            return write.origin(self, vals, **kw)
        # Read the field names *before* calling through: `write` is free to
        # normalise or consume `vals`, and what decides the set is what the
        # caller asked to change.
        written = set(vals)
        result = write.origin(self, vals, **kw)
        _dispatch_rule_change(self.env, written)
        return result

    create.origin = base_create
    write.origin = base_write
    setattr(create, _PATCH_MARKER, True)
    setattr(write, _PATCH_MARKER, True)
    BaseModel.create = create
    BaseModel.write = write
    _PATCH_INSTALLED = True
    _logger.debug(
        "Turnstone chatter adapter: installed the BaseModel.create/write layers.",
    )


_install_base_patch()


class TurnstoneTriggerChatter(models.AbstractModel):
    """Dispatch "a customer wrote on a record" to the rules that watch it.

    An `AbstractModel` for the same reason as #5752's adapter: there is no row to
    keep, no table, no access rules and no menu — a bag of behaviour hung on one
    ORM event.
    """

    _name = "turnstone.trigger.chatter"
    _description = "Turnstone Chatter Trigger"

    # -- registry wiring ----------------------------------------------------

    def _register_hook(self):
        """Learn which models any active chatter rule watches.

        Called once per registry setup and again after every module install or
        upgrade. The `BaseModel` layers themselves are installed at import; this
        only refreshes the data they read.
        """
        self._refresh_watch_list()

    def _unregister_hook(self):
        """Forget the watched models; leave the `BaseModel` layers installed.

        Removing that layer would take every layer installed on top of it with
        it — the failure `base_automation._unregister_hook()` causes for everyone
        else — and it is free when idle: an empty set means the patched function
        returns before it looks at anything.
        """
        self._set_watch_list(frozenset())

    @api.model
    def _set_watch_list(self, models_watched):
        setattr(self.env.registry, _WATCH_ATTR, frozenset(models_watched))

    @api.model
    def _refresh_watch_list(self):
        """Recompute the set of models at least one active chatter rule watches."""
        if not table_exists(self.env.cr, RULE_MODEL.replace(".", "_")):
            # A registry setup that runs while this module is still installing
            # (its table is created later in the same load) must not crash the
            # load. The next setup, or the first rule save, fills the set.
            self._set_watch_list(frozenset())
            return
        rules = self.env[RULE_MODEL].sudo().with_context(active_test=True).search([
            ("trigger_type", "in", list(CHATTER_TRIGGERS)),
        ])
        self._set_watch_list(name for name in rules.mapped("target_model") if name)
        _logger.debug(
            "Turnstone chatter adapter watches %s.",
            sorted(_watched_models(self.env.registry)),
        )

    @api.model
    def _watched_models(self):
        return _watched_models(self.env.registry)

    # -- dispatch (the entry point; the tests drive this directly too) ------

    @api.model
    def _forget_transaction_state(self):
        """Drop this transaction's re-entrancy guard. For tests.

        Not needed in production: `env.cr.cache` is discarded with the cursor,
        and a retried transaction calls `Transaction.clear()`, which empties it
        too. A `TransactionCase` is the exception — every test shares one
        transaction, so a guard left set by a nested dispatch in the first test
        would suppress the last one's event and look like a broken adapter.
        """
        self.env.cr.cache.pop(_DISPATCHING_KEY, None)

    @api.model
    def _on_messages(self, messages):
        """Queue a run for every chatter rule this batch of messages meets.

        Each rule is handled in its own guard, so an administrator's broken
        domain on one rule cannot stop another rule on the same model — the same
        decision #5752 makes, and for the same reason: one bad rule must not
        disable every good one.
        """
        for rule in self._rules_for(messages):
            try:
                for message in messages:
                    if self._is_inbound(rule, message):
                        self._queue_message(rule, message)
            except Exception:  # noqa: BLE001 - one bad rule may not stop the rest
                _logger.exception(
                    "Turnstone automation %s could not evaluate message(s) on %s; "
                    "they were posted normally, and other rules still ran.",
                    rule.name, messages[:1].model,
                )

    @api.model
    def _rules_for(self, messages):
        """The active chatter rules watching the model these messages are on.

        Searched under `sudo()` for the reason #5752 documents: the rule model is
        manager-only since #5759, and a rule has to fire for the customer who
        wrote the message, not only for a manager. `target_model` is stored and
        indexed, so this is one indexed query per model — and only for a model
        already known to be watched.
        """
        models_watched = {message.model for message in messages if message.model}
        if not models_watched:
            return self.env[RULE_MODEL]
        return self.env[RULE_MODEL].sudo().with_context(active_test=True).search([
            ("target_model", "in", sorted(models_watched)),
            ("trigger_type", "in", list(CHATTER_TRIGGERS)),
        ])

    # -- is this message a customer writing something? ----------------------

    @api.model
    def _is_inbound(self, rule, message):
        """Whether `message` is a customer writing on a record `rule` watches.

        The whole loop-suppression story is this method. It answers four
        questions, in the order that costs least:

        1. Is it a *kind* of message a customer writes? Internal notes, system
           notifications and outbound mail are not.
        2. Did somebody outside write it? Odoo's own classification, from
           `base_automation.make_message_post()`: no author (it came from
           outside) or a share partner (a portal customer).
        3. Is the author one of this deployment's own automation identities?
           An internal-user agent is already excluded by (2); this catches a
           deployment that gave the automation a portal partner, which (2) would
           otherwise call a customer.
        4. Does the record pass the rule's company wall and its domain? Reused
           from #5752's adapter, so a chatter rule and a record rule mean the
           same thing by "this record".
        """
        message = message.sudo()
        if not self._is_customer_message(message):
            return False
        if not self._is_inbound_author(message):
            return False
        if self._is_automation_author(rule, message):
            return False
        record = self._message_record(message)
        if not record:
            # The document is gone: a message can outlive the record it was
            # posted on (a deleted ticket's chatter). There is nothing to render
            # a prompt from and nothing to answer on.
            return False
        # `_may_fire()` is #5752's single-record company + domain check, reused
        # rather than copied: "does this record fire this rule" must mean one
        # thing, or the same rule behaves differently depending on whether the
        # customer typed a word or edited a field.
        return self.env["turnstone.trigger.record"]._may_fire(rule, record)

    @api.model
    def _is_customer_message(self, message):
        """Whether this message is the *kind* of thing a customer writes.

        Two independent negatives, both needed:

        * `is_internal` / an internal subtype. The chatter composer's "Log note"
          and the gateway's reply-to-a-note path both select `mail.mt_note`,
          whose `internal` flag is `True`; `is_internal` is the same statement on
          the message itself. A note is an employee talking to employees and is
          never the customer responding.
        * `message_type` in `NOT_INBOUND_TYPES` — the values Odoo's own mail
          trigger skips, plus outbound mail and out-of-office replies.
        """
        if message.is_internal or message.subtype_id.internal:
            return False
        return message.message_type not in NOT_INBOUND_TYPES

    @api.model
    def _is_inbound_author(self, message):
        """Whether somebody *outside* wrote this message.

        Odoo's own rule, taken from `base_automation.make_message_post()` so this
        adapter and a `base.automation` rule on the same model agree about what
        "received" means: no author means it came from outside (the gateway posts
        with the odoobot user and an empty author when the sender matched no
        partner), and a *share* partner is a portal or public user — a customer,
        not an employee.

        The consequence that closes the loop: an agent writing back through the
        Odoo MCP server is an internal user, its partner has
        `partner_share == False`, and its message is outbound. It cannot re-fire
        this adapter.
        """
        author = message.author_id
        if not author:
            return True
        return bool(author.partner_share)

    @api.model
    def _is_automation_author(self, rule, message):
        """Whether this message was written by this deployment's own automation.

        A second wall behind `_is_inbound_author()`, not a replacement for it.
        The common case — an internal-user agent — is already outbound because
        its partner is not a share partner. This catches the deployment that gave
        its automation a *portal* identity, where the author would look exactly
        like a customer to the test above, and where the loop would otherwise
        close for real.

        Both identities are checked: the per-rule `run_as_user_id` (when the rule
        pins a user) and the installation-wide automation user
        (`turnstone_integration.automation_user_id`).
        """
        author = message.author_id
        if not author:
            return False
        identities = self.env["res.partner"].sudo()
        if rule.run_as == "fixed_user" and rule.run_as_user_id:
            identities |= rule.run_as_user_id.partner_id
        raw = self.env["ir.config_parameter"].sudo().get_param(
            AUTOMATION_USER_PARAM
        )
        if raw:
            automation_user = self.env["res.users"].sudo().browse(
                int(raw)
            ).exists() if raw.isdigit() else None
            if automation_user:
                identities |= automation_user.partner_id
        return bool(identities) and author in identities

    @api.model
    def _message_record(self, message):
        """The business record this message is attached to, or nothing.

        One message, one record: `model`/`res_id` is a plain text column plus an
        integer, so a message can outlive its record or name a model another
        module uninstalled. Both cases answer "nothing" rather than raising — a
        customer's reply must not fail because of a message about a ticket
        somebody deleted five seconds earlier.
        """
        if not message.model or not message.res_id:
            return None
        model = self.env.get(message.model)
        if model is None:
            return None
        found = model.sudo().browse(message.res_id).exists()
        return found or None

    # -- queueing one run ---------------------------------------------------

    @api.model
    def _queue_message(self, rule, message):
        """Queue one run for `rule` about one message, or find it already queued.

        The queueing itself is #5752's `_enqueue()`: the per-transaction bound,
        the company wall and the race-safe run-key insert are one
        implementation, and a chatter run draws on the same budget as a record
        run in the same transaction. What differs is only the identity:

        * `idempotency_key` — `chatter:<mail.message id>`, the event.
        * the record the run points at — the business record, not the message, so
          a launch can render the rule's prompt and answer on the record.

        `_enqueue()` defaults `dedupe_token` to the record, which is what the
        rule's repeat window wants: for a conversation on one ticket, "this
        thing" is the ticket.
        """
        record = self._message_record(message)
        if not record:
            return self.env["turnstone.automation.run"]
        run = self.env["turnstone.trigger.record"]._enqueue(
            rule, record,
            idempotency_key=self.event_key(message),
            matched=True,
        )
        if run:
            self._keep_prompt(rule, run, message)
        return run

    @staticmethod
    def event_key(message):
        """The idempotency key for one message: the message's own identity.

        A `mail.message` row is created once, so its id names one event forever.
        A re-delivered event — a retried transaction, a gateway delivery that
        arrives twice, a form double-submitted — arrives with the same key, and
        `unique(automation_id, idempotency_key)` answers it with the run the
        first delivery queued. That is criterion 3, and it is why the key is not
        derived from the body: two different messages can say the same thing.
        """
        return "chatter:%s" % (message.id,)

    @api.model
    def _keep_prompt(self, rule, run, message):
        """Store the composed prompt on the run — only if the rule opted in.

        `run._record_prompt()` is the run model's own retention gate: it writes
        nothing and returns `False` when the rule's "Keep the Rendered Prompt" is
        off, and a direct `prompt` write on such a rule raises. Sending the prompt
        is always allowed; keeping it is the rule's decision, which is why
        `prompt_for_run()` composes it again at launch rather than depending on
        it having been stored here.

        Its own guard, because a prompt that will not render is a configuration
        problem: it must not lose the event that is already queued.
        """
        try:
            run._record_prompt(self.prompt_for_run(run, message))
        except Exception:  # noqa: BLE001 - a queued run is worth more than a kept prompt
            _logger.warning(
                "Turnstone automation %s queued run %s for message %s but could "
                "not store the composed prompt; the run is queued and will "
                "compose it again when it launches.",
                rule.name, run.id, message.id, exc_info=True,
            )

    # -- the prompt ---------------------------------------------------------

    @api.model
    def prompt_for_run(self, run, message=None):
        """The message body a run of this rule should send, composed now.

        **This is the seam #5754's drainer uses.** A `chatter_message` rule's
        prompt template is only the *instruction*: the customer's words arrive as
        the event, not as a field of the record, so they cannot be reached by
        `{{ }}` and must be appended. A drainer that calls
        `rule._render_prompt(record)` alone would reach the agent with an
        instruction and no message to act on.

        ``message``
            The triggering `mail.message`. When it is not given — the drainer only
            has a run row — it is found from the run's idempotency key, which is
            where the message id is kept.

        Returns the instruction, a blank line, and a fenced block carrying the
        author, the date and the body. **Not the thread:** the ticket asks for
        the message and its author, and a whole conversation would spend the
        agent's context on history it can fetch itself.

        The fence is longer than any run of backticks **inside** the body,
        because CommonMark closes a fence only on a run at least as long — a
        message containing ``` would otherwise end the block early and put its
        own words back at instruction level, which is exactly the hole the fence
        exists to close. Same reasoning and same shape as `_webhook_prompt()`.
        """
        record = self._run_record(run)
        instruction = run.automation_id._render_prompt(record)
        message = message or self._message_for_run(run)
        if not message:
            # Nothing to append: the instruction alone is all this rule can say.
            # Refusing here would lose an event that already happened.
            return instruction
        body = self._message_body(message)
        longest = max(
            (len(run_) for run_ in _BACKTICKS_RE.findall(body)), default=0,
        )
        fence = "`" * max(3, longest + 1)
        return "%s\n\n%s\n%s\n%s" % (instruction, fence, body, fence)

    @api.model
    def _message_for_run(self, run):
        """The message a run was queued for, from its idempotency key.

        The key is `chatter:<id>`; nothing else on the run can name the message,
        because `trigger_res_id` is the business record (see the module
        docstring). An unparseable or missing key — a run created by another path
        — answers "nothing" rather than raising.
        """
        key = run.idempotency_key or ""
        if not key.startswith("chatter:"):
            return None
        raw = key[len("chatter:"):]
        if not raw.isdigit():
            return None
        return self.env[MESSAGE_MODEL].sudo().browse(int(raw)).exists() or None

    @api.model
    def _run_record(self, run):
        """The business record a run is about, for rendering its template."""
        if not run.trigger_res_model or not run.trigger_res_id:
            return None
        model = self.env.get(run.trigger_res_model)
        if model is None:
            return None
        return model.sudo().browse(run.trigger_res_id).exists() or None

    @api.model
    def _message_body(self, message):
        """The author, the date and the body — as plain text, bounded.

        HTML that a customer typed is markup the agent would otherwise spend
        context reading around, so the body is converted to text. The date is the
        message's own timestamp, not "now": an event can be handled after the
        customer wrote it, and the agent should not be told the wrong time.
        """
        author = message.author_id.display_name or message.email_from or "unknown"
        when = fields.Datetime.to_string(message.date) if message.date else "unknown"
        body = html2plaintext(message.body) if message.body else ""
        body = body.strip() or "(the message has no text)"
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n… [truncated]"
        return "From: %s\nDate: %s\n\n%s" % (author, when, body)
