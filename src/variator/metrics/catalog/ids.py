"""Knowledge-base entity ids of history records.

The rules are those of the KB adapters (``kb/app/kb/connectors``), checked against them:

- ``sim.py`` ``sim_entity_id``: a simulated record ``X`` -> ``sim.X`` (unsafe characters -> "-");
  an id that already is a KB id (``jira.``, ``mail.``, ...) stays as it is.
- ``jira.py``: an issue key -> ``jira.<KEY>``.
- ``gitlab.py`` ``_identity``: merge request ``<project>!<iid>`` -> ``gitlab.<project, "/" -> "-">.mr-<iid>``.
- ``email.py`` ``message_entity_id``: the payload ``id`` (``msg-000123``) -> ``mail.msg-000123``.
- ``directory.py``: a person or contact ``LW-E03`` -> ``person.lw-e03``;
  an organisation ``CL-001`` -> ``org.CL-001``.
"""

from __future__ import annotations

import re

_UNSAFE = re.compile(r"[^0-9A-Za-z_.@-]+")
_KB_PREFIX = re.compile(r"^(?:sim|jira|confluence|gitlab|mail|person|org)\.\S+$")
KB_ID = re.compile(
    r"^(?:sim\.[0-9A-Za-z_.@-]+"
    r"|jira\.[A-Z][A-Z0-9_]*-\d+"
    r"|gitlab\.[0-9A-Za-z_.@-]+\.mr-\d+"
    r"|mail\.[0-9A-Za-z_.@-]+"
    r"|person\.[a-z0-9_.@-]+"
    r"|org\.[0-9A-Za-z_.@-]+"
    r"|confluence\.page-\d+)$"
)


def sim_id(raw: object) -> str:
    s = str(raw).strip()
    if _KB_PREFIX.match(s):
        return s
    return f"sim.{_UNSAFE.sub('-', s).strip('-') or 'x'}"


def jira_id(key: str) -> str:
    return f"jira.{key}"


def mr_id(project: str, iid: int | str) -> str:
    return f"gitlab.{project.strip('/').replace('/', '-')}.mr-{iid}"


def mail_id(message: str) -> str:
    s = _UNSAFE.sub("-", str(message).strip().strip("<>")).strip("-.")
    return f"mail.{s or 'unknown'}"


def person_id(pid: str) -> str:
    return f"person.{pid.lower()}"


def org_id(oid: str) -> str:
    s = str(oid)
    return f"org.{s[4:] if s.lower().startswith('org-') and len(s) > 4 else s}"


def cause_ref(raw: str) -> tuple[str, str]:
    """A dataset cause "type:id" -> (type, KB entity id of the cause)."""
    ctype, _, cid = str(raw).partition(":")
    return ctype, sim_id(cid)


def is_kb_id(value: str) -> bool:
    return bool(KB_ID.match(value))
