"""Habits of the actors, drawn around the cohort values of ``Truth`` (09 §2.3).

Each person and each client gets its own parameters: the cohort value times a log-normal
factor (partial pooling: the actor = the group + an individual deviation). Key clients
deviate more than the cohort clients. All draws come from keyed streams
``habits:<actor>:<purpose>``, so adding an actor does not move the draws of another one.

``expand_cohort`` turns the cohort parameters into ~100 individual clients with names,
contacts and contracts. ``NamePool`` gives English names to people, contacts and
companies that the spec does not name.
"""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel, ConfigDict

from variator.orgsim.company.spec import ENGINEER_ROLES, CompanySpec, Contact, KeyClient, Person
from variator.orgsim.streams import Streams

FIRST_NAMES = [
    "Adam",
    "Adaeze",
    "Alice",
    "Amara",
    "Amelia",
    "Ana",
    "Ben",
    "Bethany",
    "Caleb",
    "Carys",
    "Charlotte",
    "Chen",
    "Clara",
    "Connor",
    "Dalia",
    "Daniela",
    "Declan",
    "Divya",
    "Eden",
    "Edward",
    "Eilidh",
    "Ella",
    "Emeka",
    "Erin",
    "Ethan",
    "Fatima",
    "Felix",
    "Freya",
    "Gabriel",
    "Georgia",
    "Hamza",
    "Harvey",
    "Hollie",
    "Ibrahim",
    "Ivy",
    "Jack",
    "Jade",
    "James",
    "Jonah",
    "Kai",
    "Kamala",
    "Katie",
    "Kieran",
    "Laila",
    "Lars",
    "Lewis",
    "Lily",
    "Logan",
    "Lucas",
    "Maeve",
    "Maya",
    "Mohammed",
    "Nadia",
    "Nathan",
    "Niamh",
    "Nikhil",
    "Noah",
    "Nora",
    "Omar",
    "Owen",
    "Paige",
    "Ravi",
    "Rebecca",
    "Reuben",
    "Rosa",
    "Ruby",
    "Ryan",
    "Saanvi",
    "Sam",
    "Sara",
    "Seb",
    "Shona",
    "Simone",
    "Stefan",
    "Tariq",
    "Tess",
    "Theo",
    "Uma",
    "Victor",
    "Wren",
    "Yara",
    "Yusuf",
    "Zara",
    "Zoe",
]
LAST_NAMES = [
    "Abbott",
    "Adeyemi",
    "Ahmed",
    "Ainsworth",
    "Baker",
    "Barker",
    "Bhatt",
    "Blackwood",
    "Bowen",
    "Campbell",
    "Carter",
    "Chaudhry",
    "Clarke",
    "Coates",
    "Costa",
    "Dawson",
    "Dixon",
    "Driscoll",
    "Duffy",
    "Ellis",
    "Evans",
    "Farrell",
    "Fischer",
    "Fletcher",
    "Forde",
    "Garcia",
    "Gill",
    "Goodwin",
    "Gray",
    "Hassan",
    "Hayes",
    "Hendry",
    "Hughes",
    "Iqbal",
    "Jensen",
    "Joshi",
    "Kaur",
    "Keane",
    "Kerr",
    "Khan",
    "Lambert",
    "Lindqvist",
    "Lowe",
    "Lyons",
    "Mackay",
    "Malik",
    "Marsh",
    "Moreno",
    "Morrison",
    "Murray",
    "Nash",
    "Nolan",
    "Novak",
    "Nwosu",
    "O'Brien",
    "Okoro",
    "Osei",
    "Parry",
    "Pearce",
    "Quinlan",
    "Rahim",
    "Reid",
    "Rossi",
    "Russo",
    "Saunders",
    "Shah",
    "Sinclair",
    "Singh",
    "Stewart",
    "Sutherland",
    "Tan",
    "Thorne",
    "Turner",
    "Varga",
    "Walsh",
    "Ward",
    "Webb",
    "Whelan",
    "Wilde",
    "Yates",
    "Young",
    "Zhou",
]
COMPANY_FIRST = [
    "Amber",
    "Anchor",
    "Ashdown",
    "Aurora",
    "Beacon",
    "Bellwether",
    "Birchfield",
    "Bluewater",
    "Brightwell",
    "Calder",
    "Cedar",
    "Clearpath",
    "Cobalt",
    "Copperleaf",
    "Crestline",
    "Deltawood",
    "Driftwood",
    "Eastgate",
    "Ember",
    "Fairhaven",
    "Fernhill",
    "Foxglove",
    "Granite",
    "Greystone",
    "Harbourside",
    "Heathfield",
    "Hollin",
    "Ironbridge",
    "Juniper",
    "Kestrel",
    "Kingsmere",
    "Lakeside",
    "Lantern",
    "Larkspur",
    "Lumen",
    "Maple",
    "Marlow",
    "Millbrook",
    "Northgate",
    "Oakmoor",
    "Orchard",
    "Pebble",
    "Pinnacle",
    "Quarry",
    "Redwood",
    "Riverside",
    "Rowan",
    "Saltmarsh",
    "Silverline",
    "Southgate",
    "Sterling",
    "Stonebridge",
    "Summit",
    "Thistle",
    "Tidewater",
    "Trident",
    "Upland",
    "Vale",
    "Westbury",
    "Willow",
    "Windmill",
    "Yarrow",
]
COMPANY_SECOND = {
    "logistics": ["Logistics", "Freight", "Haulage", "Shipping"],
    "healthcare": ["Health", "Care Group", "Clinics", "Medical"],
    "retail": ["Retail", "Stores", "Outfitters", "Market"],
    "energy": ["Energy", "Power", "Renewables", "Utilities"],
    "insurance": ["Insurance", "Assurance", "Underwriting", "Mutual"],
    "media": ["Media", "Studios", "Publishing", "Broadcast"],
    "manufacturing": ["Manufacturing", "Engineering", "Industries", "Components"],
    "education": ["Learning", "Academy", "Education", "Schools Trust"],
    "fintech": ["Payments", "Capital", "Finance", "Lending"],
    "travel": ["Travel", "Holidays", "Journeys", "Hospitality"],
    "public sector": ["Council Services", "Housing Trust", "Transit Authority", "Water Board"],
    "real estate": ["Properties", "Estates", "Lettings", "Homes"],
}
MAIN_TITLES = [
    "Head of IT",
    "IT Director",
    "Chief Technology Officer",
    "Head of Digital",
    "Operations Director",
    "Product Owner",
    "Engineering Manager",
]
BILLING_TITLES = [
    "Accounts Payable Lead",
    "Finance Manager",
    "Accounts Payable Specialist",
    "Finance Officer",
    "Purchase Ledger Clerk",
]
VACATION_MONTHS = (6, 7, 8, 9, 12)
VACATION_WEIGHTS = (0.10, 0.30, 0.40, 0.10, 0.10)


class _Habit(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonHabits(_Habit):
    person_id: str
    start_min: int  # local start of the working day, minutes after midnight
    velocity: float  # story points per two-week sprint at full time (engineers)
    review_days: float  # mean delay of a code review, working days
    comment_rate: float  # multiplier of the comment templates
    block_rate: float  # multiplier of the blocker hazard
    sick_spells: float  # sick spells per year
    vacation_month: int  # month of the main two-week holiday
    attrition: float  # annual probability to resign (before rules)
    growth_rate: float  # annual rate of a growth event
    rotation_rate: float  # annual rate of a team rotation (engineers)
    ticket_rate: float  # business tickets per working day (non-engineers)
    response_hours: float  # median first response to a support ticket, working hours


class ClientHabits(_Habit):
    client_id: str
    pay_delay_mean: float  # days from the invoice to the payment
    pay_delay_sd: float
    support_per_month: float
    churn_mult: float
    incident_sensitivity: float
    upsell_rate: float
    downgrade_rate: float
    late_payment_rate: float
    renewal_uplift: float
    notice_days: float
    nps_level: float = 0.0  # NPS points above or below the company level (processes.nps)
    nps_response: float = 1.0  # multiplier of the survey response rates


class ClientProfile(KeyClient):
    key: bool = False


def _lognormal(rng: np.random.Generator, sd: float) -> float:
    """A factor with median 1 and log-sd ``sd``."""
    return float(math.exp(rng.normal(0.0, sd)))


class NamePool:
    """Unique English names for people and companies, from keyed streams."""

    def __init__(self, streams: Streams, taken: set[str] | None = None):
        self.streams = streams
        self.taken = set(taken or ())

    def person(self, entity: str) -> str:
        rng = self.streams.rng("names", entity, "person")
        while True:
            name = (
                f"{FIRST_NAMES[rng.integers(len(FIRST_NAMES))]} {LAST_NAMES[rng.integers(len(LAST_NAMES))]}"
            )
            if name not in self.taken:
                self.taken.add(name)
                return name

    def company(self, entity: str, industry: str) -> str:
        rng = self.streams.rng("names", entity, "company")
        second = COMPANY_SECOND.get(industry, ["Group"])
        while True:
            name = f"{COMPANY_FIRST[rng.integers(len(COMPANY_FIRST))]} {second[rng.integers(len(second))]}"
            if name not in self.taken:
                self.taken.add(name)
                return name


def domain_of(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum()) + ".example"


def expand_cohort(spec: CompanySpec, streams: Streams, names: NamePool) -> list[ClientProfile]:
    """Key clients first, then the cohort clients CL-007.. with drawn size, terms and contacts."""
    out = [ClientProfile(**c.model_dump(), key=True) for c in spec.key_clients]
    co = spec.cohort
    t = spec.truth
    sales = [p.id for p in spec.people if p.role == "sales"]
    first = len(out) + 1
    for n in range(first, first + co.count):
        cid = f"CL-{n:03d}"
        rng = streams.rng("cohort", cid, "profile")
        industry = co.industries[int(rng.integers(len(co.industries)))]
        acv = float(np.clip(co.acv_median * math.exp(rng.normal(0, co.acv_sigma)), co.acv_min, co.acv_max))
        billing = "monthly" if rng.random() < co.monthly_share else "quarterly"
        term = int(rng.choice(co.term_months, p=np.asarray(co.term_weights) / sum(co.term_weights)))
        renewal = int(rng.integers(1, 13)) if rng.random() < t.renewal_due else None
        name = names.company(cid, industry)
        contacts = [
            Contact(
                name=names.person(f"{cid}-main"),
                title=MAIN_TITLES[int(rng.integers(len(MAIN_TITLES)))],
                kind="main",
            ),
            Contact(
                name=names.person(f"{cid}-billing"),
                title=BILLING_TITLES[int(rng.integers(len(BILLING_TITLES)))],
                kind="billing",
            ),
        ]
        out.append(
            ClientProfile(
                id=cid,
                name=name,
                domain=domain_of(name),
                industry=industry,
                acv=round(acv / 1200) * 1200,
                billing=billing,
                term_months=term,
                renewal_in_months=renewal,
                account_manager=sales[n % len(sales)],
                contacts=contacts,
                key=False,
            )
        )
    return out


def person_habits(spec: CompanySpec, p: Person, streams: Streams) -> PersonHabits:
    """Habits of one person (also used for a new hire with its slot key as ``p.id``)."""
    t = spec.truth
    sd = t.habit_sd

    def r(purpose: str) -> np.random.Generator:
        return streams.rng("habits", p.id, purpose)

    velocity = (
        t.velocity.get(p.role, 0.0) * _lognormal(r("velocity"), 0.18) if p.role in ENGINEER_ROLES else 0.0
    )
    month = int(r("vacation").choice(VACATION_MONTHS, p=VACATION_WEIGHTS))
    attrition = 0.0 if p.executive else min(t.attrition * _lognormal(r("attrition"), 0.35), 0.6)
    return PersonHabits(
        person_id=p.id,
        start_min=int(510 + 15 * r("hours").integers(0, 7)),  # 08:30 .. 10:00
        velocity=round(velocity, 2),
        review_days=round(0.4 + 1.2 * float(r("review").random()), 2),
        comment_rate=round(_lognormal(r("comments"), sd), 3),
        block_rate=round(_lognormal(r("blocks"), sd), 3),
        sick_spells=round(t.sick_spells_per_year * _lognormal(r("sick"), 0.4), 3),
        vacation_month=month,
        attrition=round(attrition, 4),
        growth_rate=round(t.growth_rate * _lognormal(r("growth"), sd) if not p.executive else 0.0, 4),
        rotation_rate=round(t.rotation_rate if p.role in ("backend", "frontend") else 0.0, 4),
        ticket_rate=round(t.biz_tickets_per_day.get(p.role, 0.0) * _lognormal(r("tickets"), sd), 4),
        response_hours=round(1.5 * _lognormal(r("response"), sd), 3),
    )


def client_habits(spec: CompanySpec, c: ClientProfile, streams: Streams) -> ClientHabits:
    t = spec.truth
    sd = t.key_habit_sd if c.key else t.habit_sd

    def r(purpose: str) -> np.random.Generator:
        return streams.rng("habits", c.id, purpose)

    base = dict(
        client_id=c.id,
        pay_delay_mean=round(float(np.clip(t.ar_days * _lognormal(r("pay"), sd), 20, 120)), 1),
        pay_delay_sd=round(4 + 8 * float(r("pay_sd").random()), 1),
        support_per_month=round(
            (t.support_per_month_key if c.key else t.support_per_month) * _lognormal(r("support"), sd), 3
        ),
        churn_mult=round(_lognormal(r("churn"), sd) * (0.6 if c.key else 1.0), 3),
        incident_sensitivity=round(_lognormal(r("incidents"), sd), 3),
        upsell_rate=round((t.upsell_rate_key if c.key else t.upsell_rate) * _lognormal(r("upsell"), sd), 4),
        downgrade_rate=round(t.downgrade_rate * _lognormal(r("downgrade"), sd), 4),
        late_payment_rate=round(t.late_payment_rate * _lognormal(r("late"), sd), 4),
        renewal_uplift=round(t.renewal_uplift_max * float(r("uplift").random()), 4),
        notice_days=round(t.notice_days * _lognormal(r("notice"), 0.2), 1),
        nps_level=round(float(r("nps").normal(0.0, t.nps_habit_sd)), 2),
        nps_response=round(_lognormal(r("nps_response"), 0.3) * (1.2 if c.key else 1.0), 3),
    )
    base.update(c.habit_overrides)
    return ClientHabits(**base)
