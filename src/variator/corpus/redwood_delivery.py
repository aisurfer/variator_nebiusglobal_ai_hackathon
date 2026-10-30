"""Build the shared Redwood directory and source-level access configuration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from variator.corpus.redwood_profile import profile_people

PROFILE_START_UTC = datetime(2026, 1, 1, tzinfo=UTC)
PROFILE_END_UTC = PROFILE_START_UTC + timedelta(days=180)
COMPANY_NAME = "Larchwood Systems"

_NAMED_CUSTOMER_CONTACTS = (
    ("Maya Chen", "maya.chen"),
    ("Liam Ortiz", "liam.ortiz"),
    ("Nora Patel", "nora.patel"),
    ("Evan Brooks", "evan.brooks"),
    ("Priya Shah", "priya.shah"),
    ("Jon Bell", "jon.bell"),
)


def _acl(*principals: str) -> dict[str, object]:
    return {"allow": [{"principal": principal} for principal in principals], "deny": [], "inherit": False}


def _account_slug(account_name: str, account_id: str) -> str:
    slug = "".join(character for character in account_name.casefold() if character.isalnum())
    return slug or account_id.casefold()


def customer_contact_identity(account_id: str, account_name: str) -> dict[str, str]:
    """Return the stable named customer contact shared by email and directory output."""
    account_key = str(account_id)
    account_number = "".join(character for character in account_key if character.isdigit())
    index = (int(account_number) - 1) % len(_NAMED_CUSTOMER_CONTACTS) if account_number else 0
    display_name, local_part = _NAMED_CUSTOMER_CONTACTS[index]
    slug = _account_slug(str(account_name), account_key)
    contact_id = f"contact-{account_key.casefold()}-named"
    return {
        "id": contact_id,
        "name": display_name,
        "role": "Customer stakeholder",
        "email": f"{local_part}@{slug}.customer.example",
        "principal": f"contact:{contact_id}",
        "organization_id": f"org-{account_key.casefold()}",
    }


def acl_for(source: str, container: str | None, row: dict[str, Any] | None = None) -> dict[str, object]:
    """Return the source ACL for an event, including closed Redwood areas."""
    del row
    normalized = str(container or "").upper()
    if normalized in {"HR", "FINANCE", "LEADERSHIP", "COMMERCIAL"}:
        group = {
            "HR": "grp:hr",
            "FINANCE": "grp:finance",
            "LEADERSHIP": "grp:leadership",
            "COMMERCIAL": "grp:sales",
        }[normalized]
        return _acl(group, "grp:management")
    if source in {"mail-redwood", "gmail-redwood"}:
        return _acl("grp:all-staff")
    if source in {"slack-redwood", "jira-redwood", "gitlab-redwood"}:
        return _acl("grp:engineering", "grp:management")
    if source == "hubspot-redwood":
        return _acl("grp:sales", "grp:management")
    return _acl("grp:all-staff")


def build_source_registry(*, legacy_containers: bool = False) -> dict[str, object]:
    """Return source containers with explicit, default-deny access rules."""
    source_specs = (
        (
            "jira-redwood",
            "jira",
            "https://redwood.atlassian.net",
            "ENG",
            ("grp:engineering", "grp:management"),
        ),
        (
            "confluence-redwood",
            "confluence",
            "https://redwood.atlassian.net/wiki",
            "ENG",
            ("grp:all-staff",),
        ),
        (
            "gitlab-redwood",
            "gitlab",
            "https://gitlab.redwood.example",
            "platform/inference",
            ("grp:engineering", "grp:management"),
        ),
        ("mail-redwood", "email", "https://graph.microsoft.com/v1.0", "mail", ("grp:all-staff",)),
        ("xwiki-redwood", "xwiki", "https://wiki.redwood.example", "Main", ("grp:all-staff",)),
        (
            "slack-redwood",
            "slack",
            "https://redwood.slack.com",
            "eng-incidents",
            ("grp:engineering", "grp:management"),
        ),
        (
            "hubspot-redwood",
            "hubspot",
            "https://api.hubapi.com",
            "sales",
            ("grp:sales", "grp:management"),
        ),
        ("gmail-redwood", "gmail", "https://gmail.googleapis.com", "mail", ("grp:all-staff",)),
        (
            "linear-redwood",
            "linear",
            "https://api.linear.app",
            "engineering",
            ("grp:engineering", "grp:management"),
        ),
        ("fireflies-redwood", "fireflies", "https://api.fireflies.ai", "meetings", ("grp:all-staff",)),
        (
            "google-drive-redwood",
            "google_drive",
            "https://www.googleapis.com/drive/v3",
            "drive",
            ("grp:all-staff",),
        ),
    )

    def containers_for(container: str, principals: tuple[str, ...]) -> object:
        containers = {
            container: {"name": container, "acl": list(principals)},
            "ENG": {"name": "Engineering", "acl": ["grp:engineering", "grp:management"]},
            "HR": {"name": "Human resources", "acl": ["grp:hr", "grp:management"]},
            "FINANCE": {"name": "Finance", "acl": ["grp:finance", "grp:management"]},
            "COMMERCIAL": {"name": "Commercial", "acl": ["grp:sales", "grp:management"]},
            "SUPPORT": {"name": "Support", "acl": ["grp:support", "grp:management"]},
            "LEADERSHIP": {"name": "Leadership", "acl": ["grp:leadership", "grp:management"]},
            "mail": {"name": "Mail", "acl": ["grp:all-staff"]},
            "meetings": {"name": "Meetings", "acl": ["grp:all-staff"]},
            "drive": {"name": "Drive", "acl": ["grp:all-staff"]},
            "engineering": {"name": "Engineering", "acl": ["grp:engineering", "grp:management"]},
        }
        if legacy_containers:
            return [{"id": container_id, **details} for container_id, details in containers.items()]
        return containers

    return {
        "tenant": {
            "id": "redwood",
            "name": COMPANY_NAME,
            "timezone": "America/Los_Angeles",
            "people_at": PROFILE_START_UTC.isoformat(),
        },
        "sources": [
            {
                "id": source_id,
                "provider": provider,
                "base_url": base_url,
                "acl": "default-deny",
                "containers": containers_for(container, principals),
            }
            for source_id, provider, base_url, container, principals in source_specs
        ],
    }


def _profile_directory_entry(index: int, person: dict[str, Any]) -> dict[str, object]:
    display_name = str(person["display_name"])
    slug = display_name.casefold().replace(" ", "-")
    group_names = [str(group) for group in person["groups"]]
    department = (
        "Executive"
        if index == 1
        else "Sales"
        if "sales" in group_names
        else "Finance"
        if index in {9, 10, 11, 12}
        else "Support"
        if index in {13, 14, 15, 16}
        else "Engineering"
    )
    if department == "Finance":
        group_names = ["all-staff", "finance"]
    elif department == "Support":
        group_names = ["all-staff", "support"]
    role = (
        "Chief Executive Officer"
        if index == 1
        else "Head of Sales"
        if index == 2
        else "Head of Engineering"
        if index == 3
        else "Finance Manager"
        if department == "Finance"
        else "Support Lead"
        if department == "Support"
        else "Sales Manager"
        if department == "Sales"
        else "Software Engineer"
    )
    email = str(person["email"])
    return {
        "id": f"RW-E{index:02d}",
        "name": display_name,
        "role": role,
        "department": department,
        "manager": None if index <= 3 else "RW-E03" if department == "Engineering" else "RW-E02",
        "email": email,
        "principal": f"user:{slug}",
        "groups": [f"grp:{group}" for group in group_names],
        "accounts": {
            "jira-redwood": f"acct:{slug}",
            "confluence-redwood": f"acct:{slug}",
            "gitlab-redwood": slug,
            "mail-redwood": email,
            "slack-redwood": f"U{index:08d}",
            "hubspot-redwood": f"hubspot-{index:03d}",
            "gmail-redwood": email,
            "linear-redwood": f"linear-{index:03d}",
            "fireflies-redwood": f"fireflies-{index:03d}",
            "google-drive-redwood": f"drive-{index:03d}",
        },
    }


def build_directory(chains: dict[str, list[dict[str, Any]]]) -> dict[str, object]:
    """Build the contract directory, using synthetic operational employees when present."""
    employee_rows = sorted(chains.get("employees", []), key=lambda row: str(row.get("employee_id", "")))
    if employee_rows:
        people = []
        for index, employee in enumerate(employee_rows, start=1):
            employee_id = str(employee["employee_id"])
            name = str(employee.get("full_name_synthetic") or f"Redwood Employee {index}")
            slug = name.casefold().replace(" ", "-")
            department = str(employee.get("department") or "engineering").replace("_", " ").title()
            group = (
                "sales"
                if department == "Sales"
                else "finance"
                if department in {"Finance", "Ops"}
                else "support"
                if department == "Support"
                else "engineering"
            )
            role_title = str(employee.get("role_title") or "").casefold()
            extra_groups = ["grp:management"] if any(
                marker in role_title for marker in ("manager", "head", "director", "chief")
            ) else []
            email = f"{employee_id.casefold()}@redwood.example"
            people.append(
                {
                    "id": f"RW-{employee_id}",
                    "name": name,
                    "role": str(employee.get("role_title") or "Employee"),
                    "department": department,
                    "manager": (f"RW-{employee['manager_id']}" if employee.get("manager_id") else None),
                    "email": email,
                    "principal": f"user:{employee_id.casefold()}",
                    "groups": ["grp:all-staff", f"grp:{group}", *extra_groups],
                    "accounts": {
                        "jira-redwood": f"acct:{employee_id}",
                        "confluence-redwood": f"acct:{employee_id}",
                        "gitlab-redwood": slug,
                        "mail-redwood": email,
                        "slack-redwood": f"U{index:08d}",
                        "hubspot-redwood": f"hubspot-{employee_id}",
                        "gmail-redwood": email,
                        "linear-redwood": f"linear-{employee_id}",
                        "fireflies-redwood": f"fireflies-{employee_id}",
                        "google-drive-redwood": f"drive-{employee_id}",
                    },
                }
            )
    else:
        people = [
            _profile_directory_entry(index, person) for index, person in enumerate(profile_people(), start=1)
        ]

    if len(people) < len(profile_people()):
        existing_ids = {str(person["id"]) for person in people}
        existing_names = {str(person["name"]) for person in people}
        for index, profile in enumerate(profile_people(), start=1):
            candidate = _profile_directory_entry(index, profile)
            if str(candidate["id"]) in existing_ids or str(candidate["name"]) in existing_names:
                continue
            people.append(candidate)
            existing_ids.add(str(candidate["id"]))
            existing_names.add(str(candidate["name"]))
            if len(people) == len(profile_people()):
                break

    memberships_by_person: dict[str, list[dict[str, object]]] = {}
    for membership in chains.get("channel_memberships", []):
        employee_id = membership.get("employee_id")
        if not employee_id or not membership.get("joined_at"):
            continue
        memberships_by_person.setdefault(f"RW-{employee_id}", []).append(
            {
                "group": f"grp:channel-{membership.get('channel_id')}",
                "valid_from": membership["joined_at"],
                "valid_to": membership.get("left_at"),
            }
        )
    for index, person in enumerate(people, start=1):
        person_id = str(person["id"])
        department = str(person.get("department", "Engineering")).casefold()
        base_groups = ["grp:all-staff"]
        if department.startswith("sales"):
            base_groups.append("grp:sales")
        elif department.startswith(("finance", "ops")):
            base_groups.append("grp:finance")
        elif department.startswith("support"):
            base_groups.append("grp:support")
        elif department.startswith("executive"):
            base_groups.extend(("grp:leadership", "grp:management"))
        else:
            base_groups.append("grp:engineering")
        role = str(person.get("role") or "").casefold()
        if any(marker in role for marker in ("manager", "head", "director", "chief")):
            base_groups.append("grp:management")
        historical = [
            {
                "group": group,
                "valid_from": (PROFILE_START_UTC + timedelta(days=(index % 7))).isoformat(),
                "valid_to": (PROFILE_START_UTC + timedelta(days=90 + (index % 30))).isoformat()
                if index % 4 == 0
                else None,
            }
            for group in base_groups
        ]
        person["memberships"] = historical + memberships_by_person.get(person_id, [])

    customer_organizations = []
    customer_contacts = []
    for index, account in enumerate(chains.get("accounts", []), start=1):
        account_id = str(account.get("account_id") or f"A{index:03d}")
        name = str(account.get("account_name_synthetic") or account_id)
        slug = "".join(character for character in name.casefold() if character.isalnum())
        organization_id = f"org-{account_id.casefold()}"
        customer_organizations.append(
            {
                "id": organization_id,
                "name": name,
                "kind": "client" if account.get("status") == "customer" else "prospect",
                "domain": f"{slug}.customer.example",
                "accounts": {"hubspot-redwood": account_id},
            }
        )
        named_contact = customer_contact_identity(account_id, name)
        customer_contacts.append(
            {
                **named_contact,
                "accounts": {
                    "mail-redwood": named_contact["email"],
                    "hubspot-redwood": f"contact-{account_id}-named",
                },
            }
        )
        for role_slug, role_name in (
            ("finance", "Finance contact"),
            ("security", "Security contact"),
            ("support", "Support contact"),
            ("executive", "Executive sponsor"),
        ):
            email = f"{role_slug}@{slug}.customer.example"
            contact_id = f"contact-{account_id.casefold()}-{role_slug}"
            customer_contacts.append(
                {
                    "id": contact_id,
                    "name": f"{name} {role_name}",
                    "role": role_name,
                    "email": email,
                    "principal": f"contact:{contact_id}",
                    "organization_id": organization_id,
                    "accounts": {
                        "mail-redwood": email,
                        "hubspot-redwood": f"contact-{account_id}-{role_slug}",
                    },
                }
            )

    return {
        "admins": ["user:admin"],
        "groups": {
            "grp:all-staff": {"name": "All staff"},
            "grp:management": {"name": "Management"},
            "grp:engineering": {"name": "Engineering"},
            "grp:sales": {"name": "Sales"},
            "grp:hr": {"name": "Human resources"},
            "grp:finance": {"name": "Finance"},
            "grp:support": {"name": "Support"},
            "grp:leadership": {"name": "Leadership"},
        },
        "people": people,
        "organizations": [
            {
                "id": "org-redwood",
                "name": COMPANY_NAME,
                "kind": "internal",
                "domain": "redwood.example",
            },
            {
                "id": "org-redwood-partners",
                "name": "Redwood Partners",
                "kind": "partner",
                "domain": "partners.redwood.example",
            },
        ]
        + customer_organizations,
        "contacts": [
            {
                "id": f"contact:{person['id']}",
                "person_id": person["id"],
                "email": person["email"],
                "organization_id": "org-redwood",
            }
            for person in people
        ]
        + customer_contacts,
        "availability": [
            {
                "person_id": person["id"],
                "status": "available",
                "valid_from": PROFILE_START_UTC.isoformat(),
                "valid_to": PROFILE_END_UTC.isoformat(),
            }
            for person in people
        ]
        + [
            {
                "person_id": person["id"],
                "status": "vacation",
                "kind": "vacation",
                "valid_from": (PROFILE_START_UTC + timedelta(days=30 + index)).isoformat(),
                "valid_to": (PROFILE_START_UTC + timedelta(days=37 + index)).isoformat(),
            }
            for index, person in enumerate(people)
            if index % 9 == 0
        ],
    }
