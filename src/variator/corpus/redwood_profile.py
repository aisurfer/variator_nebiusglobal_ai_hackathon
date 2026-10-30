"""Map selected records to the 45-person, 180-day Redwood profile."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

PROFILE_PEOPLE = 45
PROFILE_DAYS = 180
TENANT = "redwood"
PROFILE_START = datetime(2026, 1, 1, 0, 0)


def profile_people() -> list[dict[str, Any]]:
    """Return the fixed 45-person Redwood directory in stable order."""
    names = [
        "Maya Chen",
        "Alex Rivera",
        "Jordan Patel",
        "Olivia Brooks",
        "Noah Kim",
        "Emma Stone",
        "Liam Carter",
        "Ava Singh",
        "Ethan Wright",
        "Sophia Moore",
        "Lucas Young",
        "Mia Hall",
        "Daniel Scott",
        "Isla Green",
        "Henry Adams",
        "Grace Baker",
        "Leo Nelson",
        "Chloe King",
        "Jack Turner",
        "Lily Cooper",
        "Owen Ward",
        "Ella Morris",
        "James Cook",
        "Nora Bell",
        "William Reed",
        "Amelia Ross",
        "Benjamin Gray",
        "Harper Diaz",
        "Samuel Price",
        "Evelyn Kelly",
        "Matthew Wood",
        "Sofia Bennett",
        "Michael Hughes",
        "Camila Foster",
        "Alexander Butler",
        "Charlotte Simmons",
        "Sebastian Perry",
        "Victoria Powell",
        "David Long",
        "Aria Patterson",
        "Joseph Flores",
        "Scarlett Washington",
        "Thomas Butler",
        "Penelope Bryant",
        "Charles Russell",
    ]
    people: list[dict[str, Any]] = []
    for index, name in enumerate(names, start=1):
        slug = "".join(character.lower() if character.isalnum() else "-" for character in name).strip("-")
        group = "management" if index <= 3 else "sales" if index <= 8 else "engineering"
        people.append(
            {
                "entity_id": f"{TENANT}:directory:person:{slug}",
                "kind": "person",
                "display_name": name,
                "aliases": [f"person:{slug}"],
                "email": f"{slug}@redwood.example",
                "groups": ["all-staff", group] if group != "management" else ["all-staff", "management"],
            }
        )
    return people


def world_time_transform(original: datetime, offset_days: int = 0) -> datetime:
    """Apply a deterministic day offset to a source timestamp."""
    return original + timedelta(days=offset_days)


def validate_world_time(original: datetime, world: datetime, offset_days: int) -> bool:
    """Verify the recorded deterministic calendar transform."""
    return world == world_time_transform(original, offset_days)
