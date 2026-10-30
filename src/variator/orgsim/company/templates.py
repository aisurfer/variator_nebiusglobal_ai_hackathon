"""Short English texts made by code: ticket summaries, comments, mail subjects and bodies.

The texts are placeholders with the right facts. Writer agents can replace them later: each
generated text carries a ``text_slot`` marker (see ``render``).
"""

from __future__ import annotations

import numpy as np

FEATURES = {
    "api": [
        "pagination for the orders endpoint",
        "rate limits per client key",
        "a bulk export endpoint",
        "retries for outgoing webhooks",
        "filters by date range",
        "idempotency keys for writes",
        "an audit trail for admin calls",
        "API versioning headers",
    ],
    "web-app": [
        "saved filters on the dashboard",
        "an audit log view",
        "bulk edit for users",
        "a CSV import wizard",
        "keyboard shortcuts",
        "a dark theme",
        "a usage chart on the home page",
        "inline validation on forms",
    ],
    "billing-engine": [
        "proration for mid-cycle upgrades",
        "credit notes",
        "multi-currency invoices",
        "usage-based price tiers",
        "invoice PDF templates",
        "tax rates per region",
        "dunning e-mails",
        "a billing preview",
    ],
    "data-pipeline": [
        "incremental loads for events",
        "handling of late data",
        "a schema check step",
        "daily aggregate jobs",
        "backfill tooling",
        "a dead-letter queue",
        "partition pruning",
        "data quality alerts",
    ],
    "ml-inference": [
        "a batch inference queue",
        "a GPU autoscaling policy",
        "model version pinning",
        "latency metrics per model",
        "a CPU fallback for inference",
        "warm pools for GPU nodes",
        "request batching",
        "shadow deployment of models",
    ],
    "integrations": [
        "a Salesforce sync",
        "SFTP imports",
        "Slack notifications",
        "an SAP connector",
        "signed webhooks",
        "a Teams connector",
        "a HubSpot sync",
        "an S3 export",
    ],
    "auth": [
        "SSO with SAML",
        "SCIM provisioning",
        "enforced MFA",
        "a session timeout policy",
        "API key rotation",
        "role-based access for reports",
        "login audit events",
        "password policy rules",
    ],
    "infrastructure": [
        "a Terraform module for queues",
        "staging parity with production",
        "a log retention policy",
        "a database failover drill",
        "cost alerts for GPU nodes",
        "a blue-green deploy pipeline",
        "backup restore tests",
        "a TLS certificate rotation job",
    ],
}
VERBS = ["Add", "Support", "Improve", "Build", "Allow", "Ship", "Rework", "Extend"]
QUALIFIERS = [
    "",
    "",
    " (phase 2)",
    " for large accounts",
    " behind a feature flag",
    " in the admin area",
    " for the EU region",
    " with retries",
]
SYMPTOMS = [
    "Timeout on",
    "Wrong totals in",
    "Server error from",
    "Duplicate rows in",
    "Slow response from",
    "Broken layout in",
    "Missing data in",
    "Memory leak in",
]
TASKS = [
    "Upgrade the {c} dependencies",
    "Refactor the {c} module",
    "Write a runbook for {c}",
    "Add tests for {c}",
    "Clean up feature flags in {c}",
    "Raise test coverage of {c}",
    "Remove dead code in {c}",
    "Profile memory use of {c}",
]
QA_TASKS = [
    "Automate regression tests for {c}",
    "Add end-to-end checks for {c}",
    "Update the test plan for {c}",
    "Stabilise flaky tests in {c}",
]
DEVOPS_TASKS = [
    "Tune alerts for {c}",
    "Patch base images for {c}",
    "Right-size nodes for {c}",
    "Rotate secrets of {c}",
    "Add dashboards for {c}",
    "Harden network rules for {c}",
]
DESIGN_TASKS = ["Design: {f}", "Spec: {f}", "User research: {f}", "Prototype: {f}"]
ONBOARDING_STEPS = [
    ("kick-off and requirements", 3),
    ("tenant setup", 3),
    ("SSO configuration", 3),
    ("data migration", 8),
    ("integration with the client systems", 8),
    ("user training", 2),
    ("go-live checklist", 2),
    ("hypercare after go-live", 3),
]
UPSELL_FEATURES = [
    "advanced analytics",
    "an extra region",
    "the data export module",
    "premium support",
    "the integrations pack",
    "GPU inference capacity",
    "single sign-on for partners",
    "a custom reporting suite",
]
BLOCK_REASONS = [
    "waiting for test data from the client",
    "the staging environment is down",
    "unclear acceptance criteria",
    "waiting for a decision on the API contract",
    "waiting for access to the client sandbox",
]
BIZ = {
    "ceo": [
        "Board pack for {month}",
        "Pricing review for {quarter}",
        "Approve the {quarter} budget",
        "Prepare the investor update for {month}",
        "Strategy offsite agenda for {quarter}",
    ],
    "finance": [
        "Month-end close for {month}",
        "Vendor bill review for {month}",
        "Cash forecast for {month}",
        "Expense report review for {month}",
        "Update the budget model for {quarter}",
    ],
    "delivery": [
        "Capacity plan for {quarter}",
        "Delivery review for {month}",
        "Update the on-call rota",
        "Engineering hiring plan for {quarter}",
    ],
    "pm": [
        "Status report for {client}",
        "Risk review for {client}",
        "Sprint review notes for {month}",
        "Update the delivery roadmap for {quarter}",
    ],
    "support": ["Knowledge base article: {topic}", "Support macros review", "Support metrics for {month}"],
    "sales": [
        "Account review with {client}",
        "Pipeline review for {month}",
        "Update the price book",
        "Reference call for {client}",
    ],
    "marketing": [
        "LinkedIn post: {topic}",
        "Case study draft: {client}",
        "Webinar promotion: {topic}",
        "Newsletter for {month}",
        "Blog post: {topic}",
        "Event booth plan for {quarter}",
    ],
    "analyst_designer": ["Requirements workshop with {client}", "Process map for {client}"],
}
TOPICS = [
    "GPU costs",
    "platform reliability",
    "data migrations",
    "single sign-on",
    "release notes",
    "billing automation",
    "AI inference at scale",
    "customer onboarding",
    "observability",
    "security reviews",
]
SUPPORT_SUBJECTS = {
    "how_to": [
        "How do I export the monthly report?",
        "Question about user roles",
        "How to set up a webhook?",
        "Where can I change notification settings?",
    ],
    "bug": [
        "Error when saving a filter",
        "Report totals look wrong",
        "Page does not load",
        "Export file is empty",
    ],
    "access": [
        "Cannot log in after the password reset",
        "New user needs access",
        "SSO login fails for one user",
    ],
    "data_request": [
        "Request for a data extract",
        "Please restore deleted records",
        "Need an audit log export",
    ],
    "billing": [
        "Question about the last invoice",
        "Please update our billing address",
        "PO number on invoices",
    ],
    "feature_request": [
        "Feature request: scheduled exports",
        "Feature request: more chart types",
        "Feature request: bulk user import",
    ],
    "incident": ["Service unavailable", "Very slow responses", "Errors on all pages"],
}
INCIDENT_SYMPTOMS = [
    "elevated error rate",
    "high latency",
    "failed logins",
    "stalled data loads",
    "GPU inference timeouts",
    "payment run failures",
    "dashboard outage",
]


def pick(rng: np.random.Generator, items: list) -> object:
    return items[int(rng.integers(len(items)))]


def feature(rng: np.random.Generator, component: str) -> str:
    return str(pick(rng, FEATURES.get(component, FEATURES["api"])))


def story_summary(rng: np.random.Generator, component: str) -> str:
    return f"{pick(rng, VERBS)} {feature(rng, component)}{pick(rng, QUALIFIERS)}"


def bug_summary(rng: np.random.Generator, component: str) -> str:
    return f"{pick(rng, SYMPTOMS)} {component}: {feature(rng, component)}"


def task_summary(rng: np.random.Generator, component: str, role: str) -> str:
    pool = QA_TASKS if role == "qa" else DEVOPS_TASKS if role == "devops" else TASKS
    return str(pick(rng, pool)).format(c=component)


def biz_summary(rng: np.random.Generator, role: str, month: str, quarter: str, client: str) -> str:
    return str(pick(rng, BIZ.get(role, BIZ["pm"]))).format(
        month=month, quarter=quarter, client=client, topic=pick(rng, TOPICS)
    )


def mr_description(key: str, summary: str) -> str:
    return f"Closes {key}.\n\nChanges: {summary[0].lower() + summary[1:]}. Tests added."
