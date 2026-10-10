{
    "name": "Turnstone Integration",
    "summary": "Chat with Turnstone agents from inside Odoo, and jump to whatever they touched.",
    "description": """
Turnstone Integration
=====================

Turnstone is the agentic harness: it runs the agent loop, holds durable session
state, and gates every tool call behind a policy. This module is the UI and UX
layer on top of it — it runs no agent logic of its own and makes every decision
by asking Turnstone over its HTTP API.

What it adds
------------
* A chat interface (OWL) for talking to Turnstone agents from the Odoo backend.
* Connections to one or more Turnstone servers, with a default per company.
* Sessions mirrored as Odoo records, so they are searchable and access-controlled
  like anything else.
* Tool approvals surfaced in the chat — an agent that stops for a human stops
  here, with the tool and its arguments shown.
* Linked records: when an agent writes to Odoo through the Odoo MCP server, the
  session shows what it touched and links straight to the record.
* A read-only view of Turnstone's scheduled tasks and their run history.

The API token never reaches the browser. The OWL client talks to Odoo
controllers, which call Turnstone server-side.
""",
    "author": "InfraManiacs",
    "website": "https://github.com/InfraManiacs/odoo_turnstone_integration",
    "category": "Productivity/Agents",
    "version": "19.0.1.0.0",
    "license": "LGPL-3",
    "depends": ["base", "web", "mail"],
    "data": [
        "security/turnstone_security.xml",
        "security/ir.model.access.csv",
        "views/turnstone_connection_views.xml",
        "views/turnstone_agent_views.xml",
        "views/turnstone_audit_views.xml",
        "views/turnstone_session_views.xml",
        "views/turnstone_schedule_views.xml",
        "views/turnstone_live_views.xml",
        "views/res_config_settings_views.xml",
        "views/turnstone_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "turnstone_integration/static/src/scss/turnstone_chat.scss",
            "turnstone_integration/static/src/js/**/*.js",
            "turnstone_integration/static/src/xml/**/*.xml",
        ],
    },
    "application": True,
    "installable": True,
}
