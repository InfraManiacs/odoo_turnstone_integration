"""The automation screens, from the seat of the two people who meet them.

Two criteria are tested here, and they pull in opposite directions:

1. **An Administrator can open the screens.** "The view loads" means more than
   "the XML parsed at install". The test asks the same question the web client
   asks — ``get_views()`` for the view types an action names, through the action
   itself — and then reads a real record through the fields that came back, so a
   field the form offers but the record cannot supply fails here rather than in
   somebody's browser.
2. **A Turnstone User cannot open the automation form.** A rule is the
   configured authority of an automation: which agent, which prompt, whose
   authority a run borrows. #5759 gave the ordinary User no ACL row at all on
   ``turnstone.automation``, and this file asserts the consequence a person
   experiences: the form is refused, the API read is refused, and the menu that
   leads to it is not in their menu tree.

WHY THE REFUSAL GETS ITS OWN NEGATIVE CONTROL

"There is an access error" is the weakest form of criterion 2, because it is
also what a broken view looks like. ``TestTheRefusalIsTheAclAndNotTheView``
grants the User group the read bit, asks again, and gets the form: the refusal
comes from the missing permission, and the view itself is one a non-manager
could render. It then revokes the bit and asserts the refusal returns, so the
test cannot pass by leaving the permission behind.

The same test is why the run screens are exercised for a User too:
``turnstone.automation.run`` is deliberately readable by an ordinary User (a run
is history, not configuration — #5759). A refusal on the rule that came from
"the user can do nothing at all" would be a different and much less useful claim.

NO MOCK SERVER, AND NO FIXTURE RECORDS

Nothing here opens a socket: the claims are about view architectures, ACLs,
record rules and menu visibility, and Odoo answers all four from the database.
So this file extends a plain ``TransactionCase`` instead of
``TurnstoneTransactionCase`` — the fixture's mock-server health check would be a
dependency this claim does not have (``tests/common.py`` states the rule:
use the fixture when the claim is about a request going out). The one record it
does build is a rule plus a run, created as the Administrator, because a form
that renders with no record is only half the claim.

Tagged ``post_install`` with ``-at_install``: ``TransactionCase`` carries
``{'standard', 'at_install'}`` and ``@tagged`` *adds*, so ``post_install`` alone
would run every case twice (``testing/odoo-test-runner-behaviour.md``).
"""

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.turnstone_integration.models.turnstone_client import TurnstoneClient

RULE = "turnstone.automation"
RUNMODEL = "turnstone.automation.run"

#: The models these screens are for, and the action each menu item opens.
#: Read from the xmlids rather than by browsing, so a renamed record fails here
#: instead of being silently replaced by whatever view Odoo would default to.
ACTIONS = {
    RULE: ("turnstone_integration.action_turnstone_automation",
           "turnstone_integration.view_turnstone_automation_form",
           "turnstone_integration.view_turnstone_automation_list",
           "turnstone_integration.view_turnstone_automation_search"),
    RUNMODEL: ("turnstone_integration.action_turnstone_automation_run",
               "turnstone_integration.view_turnstone_automation_run_form",
               "turnstone_integration.view_turnstone_automation_run_list",
               "turnstone_integration.view_turnstone_automation_run_search"),
}

#: The three view types every screen in this file declares. The web client asks
#: for the action's `view_mode` plus the search view it puts above the list.
VIEW_TYPES = ("list", "form", "search")


def _root(arch):
    """The arch Odoo returned, parsed, ready to be walked."""
    return etree.fromstring(arch)


def _own_fields(root):
    """Field names the view asks for from *its own* model.

    A form embeds the subview of an x2many field, and those fields belong to the
    comodel (``run_ids`` brings `turnstone.automation.run`'s columns onto the
    rule form). They are part of what renders, but not part of a rule's record,
    so a caller reading the rule must not ask for them — that distinction is the
    one this helper draws: a field nested inside another ``<field>`` is the
    subview's.
    """
    names = []
    for node in root.iter("field"):
        name = node.get("name")
        if not name or name in names:
            continue
        if any(ancestor.tag == "field" for ancestor in node.iterancestors()
               if ancestor is not root):
            continue
        names.append(name)
    return names


def _embedded(root, field_name):
    """The subview a form embedded for an x2many field, or None."""
    nodes = root.xpath("//field[@name='%s']" % field_name)
    if not nodes:
        return None
    for child in nodes[0]:
        if child.tag in ("list", "form", "kanban"):
            return child
    return None


class AutomationUiCase(TransactionCase):
    """The two users, and the one rule + run the screens are shown with."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(
            cls.env, login="automation-ui-manager", name="Automation UI Manager",
            groups="base.group_user,turnstone_integration.group_turnstone_manager",
        )
        # A Turnstone User: may chat, may read run history, may not see a rule.
        cls.user = new_test_user(
            cls.env, login="automation-ui-user", name="Automation UI User",
            groups="base.group_user,turnstone_integration.group_turnstone_user",
        )
        # Internal, with no Turnstone group at all: refused even further out.
        cls.outsider = new_test_user(
            cls.env, login="automation-ui-outsider", name="Automation UI Outsider",
            groups="base.group_user",
        )

        # No request is made here: a connection row is a URL and a token, and
        # TurnstoneClient is only constructed when something calls the server.
        cls.connection = cls.env["turnstone.connection"].create({
            "name": "Automation UI connection",
            "base_url": "http://turnstone.invalid:8099",
            "api_token": "not-a-real-token",
            "is_default": True,
        })
        cls.agent = cls.env["turnstone.agent"].create({
            "name": "Automation UI agent",
            "connection_id": cls.connection.id,
            "kind": "interactive",
        })

        # Created AS THE MANAGER, because the point of the record is that a
        # manager may have it and a User may not. Created by superuser it would
        # still test the ACL, but it would stop proving the form's own create
        # path is usable by the person the form is for.
        #
        # A `manual` rule with a prompt that reads no record, deliberately: it
        # needs no target model, no business record to point at, and no trigger
        # adapter, so the screens under test are the only thing in play. A
        # prompt naming a field (`{{ name }}`) would be refused at create for a
        # rule with no target model — which is the model's own guard
        # (`_parse_field_path`), not something this file is about.
        cls.rule = cls.env[RULE].with_user(cls.manager).create({
            "name": "Automation UI: first response",
            "agent_id": cls.agent.id,
            "trigger_type": "manual",
            "prompt_template": "Summarise the ticket and draft a first reply.",
            "run_as": "fixed_user",
            "run_as_user_id": cls.manager.id,
        })
        cls.run_row = cls.env[RUNMODEL].with_user(cls.manager).create({
            "automation_id": cls.rule.id,
            "idempotency_key": "automation-ui-run-1",
            "trigger_res_model": "res.partner",
            "trigger_res_id": 1,
        })

    # -- helpers -----------------------------------------------------------

    def _action(self, model):
        action_xmlid = ACTIONS[model][0]
        return self.env.ref(action_xmlid).sudo()

    def _declared_views(self, model):
        """view type -> the declared view's id, from the xmlids."""
        _, form, list_, search = ACTIONS[model]
        return {
            "form": self.env.ref(form).id,
            "list": self.env.ref(list_).id,
            "search": self.env.ref(search).id,
        }

    def _load_through_action(self, model, user):
        """What the web client does when a menu item is opened, and nothing less.

        ``get_views`` is called on the action's own model, with the action's
        ``view_mode`` plus the search view the list puts above it, and with
        ``load_filters``/``toolbar`` because those are the extra pieces the
        client asks for — a view broken only in its filters still fails here.
        """
        action = self._action(model)
        views = [(False, vtype) for vtype in action.view_mode.split(",")]
        if "search" not in [vtype for _, vtype in views]:
            views.append((False, "search"))
        return self.env[model].with_user(user).get_views(
            views,
            {"action_id": action.id, "load_filters": True, "toolbar": True},
        )

    def _read_form(self, record, model, user):
        """Read `record` through exactly the fields its form asks for.

        This is the half of "the view renders" that a returned arch does not
        cover: the client then reads the record with those field names, and a
        field the model cannot supply — a typo, a removed field, a field the
        user may not read — fails here rather than in a browser console.

        The spec is built per field type, because ``web_read`` answers a bare
        id when a relational field is asked for with an empty spec: that is not
        a broken read, it is the client saying "I did not ask for the contents".
        Asking for the display name of a many2one and the ids of an x2many is
        what the client does, so the payload below is the shape a form actually
        renders from — and the assertions on ``['id']``/``['display_name']``
        would be meaningless against the bare-int form.
        """
        result = self.env[model].with_user(user).get_views([(False, "form")])
        arch = _root(result["views"]["form"]["arch"])
        own = _own_fields(arch)
        known = result["models"][model]["fields"]
        # Every field the form offers must be one the model actually has; the
        # intersection with `models` is what Odoo itself trusts.
        self.assertEqual(
            sorted(set(own) - set(known)), [],
            "the %s form names fields the model does not return" % model,
        )
        spec = {}
        for name in own:
            ftype = known[name].get("type")
            if ftype == "many2one":
                spec[name] = {"fields": {"display_name": {}}}
            elif ftype in ("one2many", "many2many"):
                spec[name] = {"fields": {"id": {}}}
            else:
                spec[name] = {}
        return record.with_user(user).web_read(spec)


@tagged("standard", "-at_install", "post_install")
class TestManagerCanOpenTheAutomationScreens(AutomationUiCase):
    """Criterion 1: the views render, for the person they are built for."""

    def test_both_menu_actions_load_every_declared_view(self):
        """Each action resolves to its declared list, form and search view.

        The id assertion is what makes this about *these* screens: an action
        whose ``view_mode`` is wrong, or whose model gained no views of its own,
        would still return an arch — a default one generated by Odoo. Equality
        with the xmlid is the difference between "something rendered" and "the
        screen #5760 wrote rendered".
        """
        for model in (RULE, RUNMODEL):
            with self.subTest(model=model):
                result = self._load_through_action(model, self.manager)
                self.assertEqual(sorted(result["views"]), sorted(VIEW_TYPES))

                expected = self._declared_views(model)
                for vtype in VIEW_TYPES:
                    view = result["views"][vtype]
                    self.assertEqual(
                        view["id"], expected[vtype],
                        "%s's %s view is not the declared one" % (model, vtype),
                    )
                    self.assertEqual(_root(view["arch"]).tag, vtype)

                # The models dict is what the client renders from: it must
                # describe the view's own model (and, for the rule form, the
                # run model its embedded list shows).
                self.assertIn(model, result["models"])
                self.assertTrue(result["models"][model]["fields"])

    def test_the_rule_form_offers_the_whole_configuration(self):
        """The three questions a rule answers are all on the form.

        A field missing from this form is a field nobody can set: the model is
        Administrator-only, so the form is its only UI. The list is deliberately
        the short one — when it fires, what it asks, who it runs as — because
        asserting every field would only restate the XML.
        """
        result = self.env[RULE].with_user(self.manager).get_views([(False, "form")])
        root = _root(result["views"]["form"]["arch"])
        own = _own_fields(root)

        for name in ("name", "agent_id", "trigger_type", "target_model_id",
                     "prompt_template", "run_as", "run_as_user_id"):
            self.assertIn(name, own, "the rule form does not offer %s" % name)

        # The prompt field's widget is what makes `{{ field }}` writable by a
        # person rather than by guesswork, and the runs tab is the rule's own
        # view of its history. Both are #5760's decisions, asserted here because
        # a form that lost either still loads.
        prompt = root.xpath("//field[@name='prompt_template']")[0]
        self.assertEqual(prompt.get("widget"), "turnstone_prompt_template")

        runs_field = root.xpath("//field[@name='run_ids']")
        self.assertTrue(runs_field, "the rule form has no Runs tab")
        # The Runs tab is read-only because the FIELD is (`readonly=True` on the
        # model), not because the arch repeats it — so the arch carries no
        # `readonly` attribute and asserting one would be asserting a decoration
        # that is not there. The model's own answer is the one that holds.
        self.assertTrue(
            result["models"][RULE]["fields"]["run_ids"]["readonly"],
            "run_ids is not read-only on the model, so its tab is editable",
        )

    def test_the_run_history_tab_is_read_only(self):
        """A run is history: the embedded list offers no way to make or change one.

        The launch service is the only writer (#5749), and the form has to say so
        by construction — not by the model refusing a write only after somebody
        has typed into a cell.
        """
        result = self.env[RULE].with_user(self.manager).get_views([(False, "form")])
        list_view = _embedded(_root(result["views"]["form"]["arch"]), "run_ids")
        self.assertIsNotNone(list_view, "run_ids embeds no list view")
        self.assertEqual(list_view.tag, "list")
        for attribute in ("create", "delete", "edit"):
            self.assertEqual(
                list_view.get(attribute), "false",
                "the rule form's run list is %s-editable" % attribute,
            )
        # And the subview it embeds is the run model's, so the columns come from
        # `turnstone.automation.run` rather than from the rule.
        self.assertIn(RUNMODEL, result["models"])

    def test_a_rule_reads_through_the_fields_its_form_shows(self):
        """A real rule, read with the form's own field names, as a manager."""
        payload, = self._read_form(self.rule, RULE, self.manager)
        self.assertEqual(payload["id"], self.rule.id)
        self.assertEqual(payload["name"], "Automation UI: first response")
        self.assertEqual(payload["agent_id"]["id"], self.agent.id)
        self.assertEqual(payload["agent_id"]["display_name"], self.agent.name)
        self.assertEqual(payload["trigger_type"], "manual")
        self.assertEqual(payload["run_as"], "fixed_user")
        self.assertEqual(payload["prompt_template"],
                         "Summarise the ticket and draft a first reply.")
        # The runs tab shows the run that exists. `run_count` is deliberately NOT
        # asserted as 1: the counter is the launch service's bookkeeping
        # (`_note_run()`), so a run row created directly — as this fixture does,
        # with no launch behind it — leaves it at 0. Asserting the counter here
        # would be asserting the fixture, not the screen.
        self.assertEqual([row["id"] for row in payload["run_ids"]], [self.run_row.id])
        self.assertEqual(payload["run_count"], 0)

    def test_a_run_reads_through_the_fields_its_form_shows(self):
        """The run record, through the run form's fields: the three things it
        exists to answer — did it fire, what about, and where did it get to."""
        payload, = self._read_form(self.run_row, RUNMODEL, self.manager)
        self.assertEqual(payload["id"], self.run_row.id)
        self.assertEqual(payload["automation_id"]["id"], self.rule.id)
        self.assertEqual(payload["automation_id"]["display_name"], self.rule.name)
        self.assertEqual(payload["state"], "queued")
        self.assertEqual(payload["trigger_res_model"], "res.partner")
        self.assertEqual(payload["trigger_res_id"], 1)


@tagged("standard", "-at_install", "post_install")
class TestNonManagerCannotOpenTheAutomationForm(AutomationUiCase):
    """Criterion 2: the rule's screens are an Administrator's, and only theirs."""

    def _assert_refused(self, user, label):
        """The form, the API read and the menu are all closed to `user`."""
        with self.assertRaises(AccessError, msg="%s opened the rule views" % label):
            self.env[RULE].with_user(user).get_views([(False, "list"), (False, "form")])
        with self.assertRaises(AccessError, msg="%s read a rule through the API" % label):
            self.rule.with_user(user).web_read({"name": {}, "prompt_template": {}})

        visible = self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()
        automations = self.env.ref("turnstone_integration.menu_turnstone_automations")
        self.assertNotIn(
            automations.id, visible,
            "%s has the Automations menu, which is the way into that form" % label,
        )

    def test_a_turnstone_user_cannot_open_the_automation_form(self):
        self._assert_refused(self.user, "a Turnstone User")

    def test_an_internal_user_with_no_turnstone_group_cannot_open_it(self):
        self._assert_refused(self.outsider, "an internal user with no Turnstone group")

    def test_the_refusal_is_the_acl_and_not_a_broken_view(self):
        """Grant the read bit, and the SAME call renders the SAME view.

        Without this, "an AccessError was raised" would be equally consistent
        with a view that does not build for this user — the two failures a
        reviewer actually cares about are a missing permission and a broken
        screen, and only this flip tells them apart. It also states the second
        half of the design: the rule form is not manager-shaped, it is
        *permission*-shaped, and an installation that grants a group the bit gets
        a working screen rather than a new bug.

        The grant is made and revoked inside one test; `TransactionCase` rolls
        the test's transaction back, but the revoke is explicit because the
        cached ACL answer must also be dropped for the refusal to be real again.

        AND IT SHOWS THE MENU IS HIDDEN BY A SECOND WALL. Granting the read bit
        does NOT reveal the Automations menu: the menu's own `groups` is what
        hides it, and the ACL is what refuses the form behind it. The two are
        independent (`architecture/menu-tree-and-visibility.md`), so the test
        removes the menu's group as well and only then asserts the menu appears.
        That is the honest statement of what a permission change buys — and the
        reason a support request that says "give them the menu" is not the same
        request as "give them the model".
        """
        declared = self._declared_views(RULE)["form"]
        menu = self.env.ref("turnstone_integration.menu_turnstone_automations")
        before = self.env[RULE].with_user(self.user)
        with self.assertRaises(AccessError):
            before.get_views([(False, "form")])

        grant = self.env["ir.model.access"].create({
            "name": "Automation UI test grant: user may read a rule",
            "model_id": self.env["ir.model"]._get(RULE).id,
            "group_id": self.env.ref("turnstone_integration.group_turnstone_user").id,
            "perm_read": True,
        })
        self.env.registry.clear_cache()
        try:
            result = before.get_views([(False, "form")])
            self.assertEqual(
                result["views"]["form"]["id"], declared,
                "with the bit granted, a User renders some other view — so the "
                "refusal above was not the permission but the view",
            )
            payload = self.rule.with_user(self.user).web_read({"name": {}})
            self.assertEqual(payload[0]["name"], self.rule.name)

            # WALL TWO: the permission is not enough. The menu is still hidden.
            self.assertNotIn(
                menu.id, self.env["ir.ui.menu"].with_user(self.user)._visible_menu_ids(),
                "the read bit alone revealed the Automations menu — the menu's "
                "group should still be hiding it",
            )

            # With the group removed too, the menu appears: the second wall is
            # the group, and the form behind it works. Both walls, one at a time.
            menu.write({"group_ids": [(5, 0, 0)]})
            self.env.registry.clear_cache()
            self.assertIn(
                menu.id, self.env["ir.ui.menu"].with_user(self.user)._visible_menu_ids(),
                "with the group and the ACL both open, the menu is still hidden",
            )
        finally:
            grant.unlink()
            self.env.registry.clear_cache()

        with self.assertRaises(AccessError, msg="revoking the bit did not re-close the form"):
            before.get_views([(False, "form")])

    def test_a_user_may_still_read_run_history(self):
        """The refusal is about rules, not about the Turnstone User being nobody.

        #5759 gave an ordinary User a read-only ACL on ``turnstone.automation.run``
        on purpose: "why did my ticket not reach an agent" is the ticket owner's
        question. Asserting it here keeps the rule refusal from being read as a
        blanket denial, and it is the positive control for this whole class.
        """
        result = self.env[RUNMODEL].with_user(self.user).get_views(
            [(False, "list"), (False, "form")])
        self.assertEqual(
            result["views"]["form"]["id"], self._declared_views(RUNMODEL)["form"])

        payload = self.run_row.with_user(self.user).web_read({
            "name": {},
            "state": {},
            # `web_read` answers a bare id for a relational field asked for with
            # an empty spec; the display name is what the run form's
            # `automation_id` column renders, so ask for that.
            "automation_id": {"fields": {"display_name": {}}},
        })
        self.assertEqual(payload[0]["id"], self.run_row.id)
        self.assertEqual(payload[0]["state"], "queued")
        self.assertEqual(payload[0]["automation_id"]["id"], self.rule.id)
        self.assertEqual(payload[0]["automation_id"]["display_name"], self.rule.name)

        # ...but a run is not theirs to write: reading history is the whole grant.
        with self.assertRaises(AccessError):
            self.run_row.with_user(self.user).write({"error": "rewritten history"})
        with self.assertRaises(AccessError):
            self.env[RUNMODEL].with_user(self.user).create({
                "automation_id": self.rule.id,
                "idempotency_key": "user-made-run",
            })

    def test_the_run_menu_stays_hidden_even_though_runs_are_readable(self):
        """A second wall, and a different one from the rule's.

        A User *can* read runs, so the group on the Runs menu is the only thing
        hiding it (``architecture/menu-tree-and-visibility.md``). Asserting it
        here is what stops "the menu is hidden" from being attributed to the ACL
        in a test that could not tell the two apart.
        """
        visible = self.env["ir.ui.menu"].with_user(self.user)._visible_menu_ids()
        for xmlid in ("turnstone_integration.menu_turnstone_automation_runs",
                      "turnstone_integration.menu_turnstone_live"):
            self.assertNotIn(self.env.ref(xmlid).id, visible, xmlid)
        # And the manager sees both, so the assertion is about the user.
        manager_visible = self.env["ir.ui.menu"].with_user(self.manager)._visible_menu_ids()
        for xmlid in ("turnstone_integration.menu_turnstone_automation_runs",
                      "turnstone_integration.menu_turnstone_automations"):
            self.assertIn(self.env.ref(xmlid).id, manager_visible, xmlid)


@tagged("standard", "-at_install", "post_install")
class TestTheScreensDoNotReachTheNetwork(AutomationUiCase):
    """Opening a screen must not talk to Turnstone.

    A view is configuration, and a form that refreshed something from the server
    on load would make the administration UI depend on the console being up —
    visible as a slow page or a traceback in an installation whose server is
    down. The client is patched to refuse construction, so any code path that
    tries to build one fails here by name rather than by timing out.
    """

    def test_loading_the_views_builds_no_client(self):
        original = TurnstoneClient.__init__

        def refuse(self, *args, **kwargs):
            raise AssertionError(
                "loading the automation screens constructed a TurnstoneClient — "
                "a view must not depend on the server being reachable")

        TurnstoneClient.__init__ = refuse
        try:
            for model in (RULE, RUNMODEL):
                self._load_through_action(model, self.manager)
            self._read_form(self.rule, RULE, self.manager)
            self._read_form(self.run_row, RUNMODEL, self.manager)

            # POSITIVE CONTROL: without this, the assertions above would pass
            # just as happily if the patch were never installed — a guard that
            # is not armed proves nothing. Constructing a client here must hit
            # the guard, so the silence above is the views not building one.
            with self.assertRaises(AssertionError, msg="the guard is not in force"):
                TurnstoneClient("http://turnstone.invalid:8099", "not-a-real-token")
        finally:
            TurnstoneClient.__init__ = original