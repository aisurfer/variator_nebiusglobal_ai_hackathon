"""The company spec of the MVP and its hidden truth, as Pydantic models.

The spec says who the company is: people, departments, key clients, the client cohort,
vendors and the tax authority. ``Truth`` holds the hidden parameters of the history. The
money and the operating parameters are the defaults of the ``company_model`` template
(``variator_it_services.company``), not scaled (PRD D19). The habits of each actor are
drawn around these values by ``habits.draw_habits``.

``CompanySpec.load(path)`` and ``spec.dump(path)`` read and write the spec as JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal[
    "ceo",
    "finance",
    "delivery",
    "pm",
    "analyst_designer",
    "backend",
    "frontend",
    "devops",
    "qa",
    "support",
    "sales",
    "marketing",
]
ENGINEER_ROLES: tuple[str, ...] = ("backend", "frontend", "devops", "qa")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Company(_Model):
    name: str = "Larchwood Systems"
    tenant: str = "company"
    domain: str = "larchwood.io"
    timezone: str = "Europe/London"
    currency: str = "USD"
    sector: str = "IT services · platform development and support"
    language: Literal["en"] = "en"
    engineering_department: str = "Engineering"
    jira_base_url: str = "https://larchwood.atlassian.net"
    confluence_base_url: str = "https://larchwood.atlassian.net/wiki"
    gitlab_base_url: str = "https://git.larchwood.io"
    gitlab_project: str = "larchwood/platform"
    north_star_target_k: int = Field(14_400, description="ARR goal in $k by north_star_by (placeholder)")
    north_star_by: str = "2027-12-31"


class Department(_Model):
    id: str
    name: str
    head: str


class Person(_Model):
    """One employee at the start of the history; ``seat`` is the position the person fills."""

    id: str
    name: str
    title: str
    role: Role
    department: str
    manager: str | None = None
    login: str
    salary: float = Field(gt=0, description="annual gross salary, USD")
    tenure_months: int = Field(ge=0, description="months in the company at the start of the history")
    executive: bool = False
    key: bool = False
    skills: list[str] = Field(default_factory=list, description="components the person works on")
    role_id: str | None = Field(None, description="dashboard role (ceo, finance, delivery)")

    @property
    def seat(self) -> str:
        return self.id


class Contact(_Model):
    name: str
    title: str
    kind: Literal["main", "billing"]


class KeyClient(_Model):
    id: str
    name: str
    domain: str
    industry: str
    acv: float = Field(gt=0, description="annual contract value at the start of the history, USD")
    billing: Literal["monthly", "quarterly"] = "monthly"
    term_months: int = 12
    renewal_in_months: int | None = Field(None, description="months from the start to the renewal")
    account_manager: str
    contacts: list[Contact]
    habit_overrides: dict[str, float] = Field(default_factory=dict)


class ClientCohort(_Model):
    """The many smaller clients: drawn one by one by ``habits.expand_cohort``."""

    count: int = 99
    acv_median: float = 75_600
    acv_sigma: float = 0.6
    acv_min: float = 24_000
    acv_max: float = 300_000
    monthly_share: float = 0.7
    term_months: list[int] = [12, 24]
    term_weights: list[float] = [0.75, 0.25]
    industries: list[str] = [
        "logistics",
        "healthcare",
        "retail",
        "energy",
        "insurance",
        "media",
        "manufacturing",
        "education",
        "fintech",
        "travel",
        "public sector",
        "real estate",
    ]


class Vendor(_Model):
    id: str
    name: str
    kind: Literal["nearshore", "offshore", "cloud", "landlord", "saas", "professional", "marketing", "bank"]
    domain: str
    monthly_fee: float = Field(ge=0, description="USD per month (cloud: see Truth.infra_base)")
    terms_days: int = 30
    contact_name: str
    contact_title: str
    cost_class: Literal["opex", "infra", "cac", "none"] = "opex"


class TaxAuthority(_Model):
    id: str = "TA-UK"
    name: str = "HM Revenue & Customs"


class Truth(_Model):
    """Hidden parameters. The first block is the ``company_model`` defaults (mode of each PERT)."""

    # finance (company_model)
    arr_target: float = 12_000_000
    cash_target: float = 2_000_000
    acv: float = 120_000
    opex: float = 8_800_000
    budget: float = 11_200_000
    cac: float = 30_000
    ar_days: float = 60
    infra_base: float = 1_500_000
    # clients (company_model)
    base_churn: float = 0.12
    pipeline: float = 3_000_000
    win_rate: float = 0.25
    renewal_due: float = 0.40
    overdue_base: float = 0.10
    support_quality: float = 0.8
    # operations and product (company_model)
    incidents_base: float = 4.0
    mttr_h: float = 2.0
    deploy_freq: float = 10.0
    team_load: float = 0.95
    blockers: float = 0.10
    tech_debt: float = 0.45
    latency_base: float = 180.0
    latency_slo: float = 250.0
    sla_target: float = 0.995
    # people (company_model)
    attrition: float = 0.15
    hiring_lead: float = 3.0
    skills: float = 1.0
    # link coefficients (company_model)
    k_mttr_deploy: float = 0.3
    k_churn_overdue: float = 0.15
    k_churn_nps: float = Field(0.01, description="churn added per NPS point below 30, in the renewal window")
    k_lat_gpu: float = 0.6
    k_lat_debt: float = Field(0.5, description="company_model only: the history has the debt in latency_base")
    k_inc_debt: float = Field(
        1.2, description="company_model only: the history has the debt in incidents_base"
    )
    # NPS (company_model: nps = 40 - 2000 x SLA gap - 40 x overdue + 50 x (support_quality - 0.8))
    nps_base: float = 40.0
    k_nps_sla: float = 2000.0
    k_nps_overdue: float = 40.0
    k_nps_support: float = 50.0
    nps_churn_floor: float = Field(30.0, description="NPS below this level adds churn (company_model)")
    # rules (rules/company_basic.yaml)
    burnout_load: float = 1.05
    burnout_attrition_add: float = 0.05
    # process-level truth of this generator (assumptions, documented in truth.json)
    gpu_share: float = 0.35
    payroll_tax_rate: float = 0.15
    employee_deduction_rate: float = 0.30
    corporation_tax_rate: float = 0.25
    commission_rate: float = 0.08
    upsell_rate: float = 0.15
    upsell_rate_key: float = 0.45
    upsell_share: tuple[float, float] = (0.15, 0.40)
    upsell_win: float = 0.85
    downgrade_rate: float = 0.05
    downgrade_share: tuple[float, float] = (0.10, 0.30)
    late_payment_rate: float = 0.15
    late_extra_days: tuple[float, float] = (25, 55)
    late_spell_days: tuple[float, float] = (60, 120)
    notice_days: float = 60
    renewal_uplift_max: float = 0.05
    support_per_month: float = 1.0
    support_per_month_key: float = 4.0
    support_escalation: float = 0.12
    upsell_support_mult: float = 1.5
    incident_deploy_share: float = 0.6
    severity_mix: tuple[float, float, float] = (0.10, 0.35, 0.55)
    k_incident_churn: float = 0.6
    incident_churn_tau_days: float = 60
    qa_reopen: float = 0.08
    unmet_expectation_attrition_add: float = 0.10
    growth_rate: float = 0.25
    raise_grant: float = 0.6
    rotation_rate: float = 0.08
    sick_spells_per_year: float = 2.5
    vacation_days: int = 25
    notice_bdays: int = 20
    new_deal_value_median: float = 100_000
    deal_value_sigma: float = 0.5
    deal_cycle_days: tuple[float, float] = (35, 110)
    open_deals_at_start: int = 6
    implementation_bdays: tuple[int, int] = Field(
        (28, 50),
        description="promised go-live of an onboarding, working days (v2: some are late, overdue_base)",
    )
    upsell_implementation_share: float = Field(
        0.55, description="promised go-live of an upsell / of an onboarding"
    )
    velocity: dict[str, float] = Field(
        default_factory=lambda: {"backend": 17.0, "frontend": 16.0, "devops": 12.5, "qa": 10.0}
    )
    biz_tickets_per_day: dict[str, float] = Field(
        default_factory=lambda: {
            "ceo": 0.14,
            "finance": 0.55,
            "delivery": 0.22,
            "pm": 0.45,
            "analyst_designer": 0.60,
            "support": 0.18,
            "sales": 0.45,
            "marketing": 0.75,
        }
    )
    habit_sd: float = Field(0.25, description="log-sd of an individual habit around its cohort value")
    key_habit_sd: float = Field(0.35, description="log-sd for key clients")
    # NPS survey process (processes.nps)
    nps_survey_months: tuple[int, ...] = (1, 4, 7, 10)
    nps_window_days: int = Field(
        91, description="days of client experience before a survey that set the score"
    )
    nps_response_main: float = 0.55
    nps_response_billing: float = 0.30
    nps_reply_bdays: float = Field(3.0, description="mean working days from the survey to a response")
    nps_score_sd: float = Field(1.3, description="sd of a response score around the client level")
    nps_habit_sd: float = Field(18.0, description="sd of the client NPS level, NPS points")
    nps_billing_offset: float = Field(-0.1, description="score offset of a billing contact")
    nps_recency_days: float = Field(60.0, description="decay of an old SLA gap in the score")
    nps_overdue_full_bdays: int = Field(
        20, description="working days overdue that count as a fully late go-live"
    )
    nps_csat_ref: float = Field(
        4.45, description="experience score of a ticket at the normal support quality"
    )
    nps_shrink: float = Field(3.0, description="prior weight of the client NPS signal for churn, responses")
    nps_prior: float = Field(35.0, description="prior of the client NPS signal for churn")
    nps_renewal_window_days: int = Field(180, description="days before the term end when NPS adds churn")
    # monitoring (processes.monitoring)
    k_lat_load: float = Field(0.35, description="p95 latency growth per unit of traffic above the normal")
    k_lat_incident: float = Field(1.0, description="p95 growth on a day with 5% or more of degraded minutes")
    gpu_hit: tuple[float, float] = Field(
        (0.3, 0.6), description="GPU shortage severity after a capacity event"
    )
    gpu_tau_days: float = Field(7.0, description="decay of a GPU shortage, days")
    latency_noise_sd: float = 0.06


class CompanySpec(_Model):
    company: Company = Field(default_factory=Company)
    departments: list[Department]
    people: list[Person]
    key_clients: list[KeyClient]
    cohort: ClientCohort = Field(default_factory=ClientCohort)
    vendors: list[Vendor]
    tax_authority: TaxAuthority = Field(default_factory=TaxAuthority)
    truth: Truth = Field(default_factory=Truth)
    components: list[str] = [
        "api",
        "web-app",
        "billing-engine",
        "data-pipeline",
        "ml-inference",
        "integrations",
        "auth",
        "infrastructure",
    ]

    @model_validator(mode="after")
    def _check(self) -> CompanySpec:
        ids = [p.id for p in self.people]
        if len(set(ids)) != len(ids):
            raise ValueError("person ids must be unique")
        known = set(ids)
        for p in self.people:
            if p.manager and p.manager not in known:
                raise ValueError(f"{p.id}: unknown manager {p.manager}")
        for c in self.key_clients:
            if c.account_manager not in known:
                raise ValueError(f"{c.id}: unknown account manager {c.account_manager}")
        for d in self.departments:
            if d.head not in known:
                raise ValueError(f"department {d.id}: unknown head {d.head}")
        return self

    # ------------------------------------------------------------------ helpers
    def person(self, pid: str) -> Person:
        return next(p for p in self.people if p.id == pid)

    def by_role(self, role: str) -> list[Person]:
        return [p for p in self.people if p.role == role]

    def vendor(self, kind: str) -> Vendor:
        return next(v for v in self.vendors if v.kind == kind)

    # ------------------------------------------------------------------ JSON
    @classmethod
    def load(cls, path: str | Path) -> CompanySpec:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------- default
# id, name, title, role, department, manager, login, salary, tenure, flags, skills
_PEOPLE = [
    (
        "LW-E01",
        "Avery Collins",
        "Chief Executive Officer",
        "ceo",
        "Management",
        None,
        "avery.collins",
        220_000,
        96,
        "exec",
        [],
        "ceo",
    ),
    (
        "LW-E02",
        "Morgan Reyes",
        "Head of Finance",
        "finance",
        "Finance and HR",
        "LW-E01",
        "morgan.reyes",
        160_000,
        60,
        "exec",
        [],
        "finance",
    ),
    (
        "LW-E03",
        "Jordan Patel",
        "Head of Delivery",
        "delivery",
        "Delivery",
        "LW-E01",
        "jordan.patel",
        170_000,
        72,
        "exec",
        [],
        "delivery",
    ),
    (
        "LW-E04",
        "Hannah Okafor",
        "Project Manager",
        "pm",
        "Delivery",
        "LW-E03",
        "hannah.okafor",
        110_000,
        30,
        "",
        [],
        None,
    ),
    (
        "LW-E05",
        "Leo Marchetti",
        "Business Analyst and Product Designer",
        "analyst_designer",
        "Delivery",
        "LW-E03",
        "leo.marchetti",
        95_000,
        22,
        "",
        ["web-app", "api"],
        None,
    ),
    (
        "LW-E06",
        "Daniel Kowalski",
        "Lead Backend Engineer",
        "backend",
        "Engineering",
        "LW-E03",
        "daniel.kowalski",
        145_000,
        64,
        "key",
        ["api", "billing-engine", "auth"],
        None,
    ),
    (
        "LW-E07",
        "Aisha Rahman",
        "Senior Backend Engineer",
        "backend",
        "Engineering",
        "LW-E06",
        "aisha.rahman",
        130_000,
        41,
        "",
        ["billing-engine", "data-pipeline", "api"],
        None,
    ),
    (
        "LW-E08",
        "Tom Gallagher",
        "Backend Engineer",
        "backend",
        "Engineering",
        "LW-E06",
        "tom.gallagher",
        115_000,
        26,
        "",
        ["integrations", "api"],
        None,
    ),
    (
        "LW-E09",
        "Mei Lin Chow",
        "Backend Engineer",
        "backend",
        "Engineering",
        "LW-E06",
        "meilin.chow",
        112_000,
        14,
        "",
        ["data-pipeline", "ml-inference"],
        None,
    ),
    (
        "LW-E10",
        "Oliver Brennan",
        "Senior Frontend Engineer",
        "frontend",
        "Engineering",
        "LW-E06",
        "oliver.brennan",
        120_000,
        38,
        "",
        ["web-app"],
        None,
    ),
    (
        "LW-E11",
        "Sofia Almeida",
        "Frontend Engineer",
        "frontend",
        "Engineering",
        "LW-E06",
        "sofia.almeida",
        105_000,
        17,
        "",
        ["web-app"],
        None,
    ),
    (
        "LW-E12",
        "Kwame Mensah",
        "Senior DevOps Engineer",
        "devops",
        "Engineering",
        "LW-E03",
        "kwame.mensah",
        135_000,
        45,
        "key",
        ["infrastructure", "ml-inference"],
        None,
    ),
    (
        "LW-E13",
        "Rhys Llewellyn",
        "DevOps Engineer",
        "devops",
        "Engineering",
        "LW-E12",
        "rhys.llewellyn",
        115_000,
        20,
        "",
        ["infrastructure"],
        None,
    ),
    (
        "LW-E14",
        "Elena Petrova",
        "Senior QA Engineer",
        "qa",
        "Engineering",
        "LW-E06",
        "elena.petrova",
        95_000,
        35,
        "",
        ["api", "web-app", "billing-engine"],
        None,
    ),
    (
        "LW-E15",
        "Samir Haddad",
        "QA Engineer",
        "qa",
        "Engineering",
        "LW-E14",
        "samir.haddad",
        85_000,
        12,
        "",
        ["web-app", "integrations"],
        None,
    ),
    (
        "LW-E16",
        "Grace Whitfield",
        "Senior Support Engineer",
        "support",
        "Support",
        "LW-E03",
        "grace.whitfield",
        75_000,
        33,
        "",
        [],
        None,
    ),
    (
        "LW-E17",
        "Callum Fraser",
        "Support Engineer",
        "support",
        "Support",
        "LW-E16",
        "callum.fraser",
        65_000,
        15,
        "",
        [],
        None,
    ),
    (
        "LW-E18",
        "Natalie Brooks",
        "Senior Account Manager",
        "sales",
        "Sales",
        "LW-E01",
        "natalie.brooks",
        105_000,
        40,
        "",
        [],
        None,
    ),
    (
        "LW-E19",
        "Marcus Delaney",
        "Sales and Account Manager",
        "sales",
        "Sales",
        "LW-E18",
        "marcus.delaney",
        95_000,
        19,
        "",
        [],
        None,
    ),
    (
        "LW-E20",
        "Isla McKenzie",
        "Marketing Manager (Social Media)",
        "marketing",
        "Marketing",
        "LW-E01",
        "isla.mckenzie",
        80_000,
        16,
        "",
        [],
        None,
    ),
]

# id, name, domain, industry, acv, billing, term, renewal month, account manager, main and billing contacts
_KEY_CLIENTS = [
    (
        "CL-001",
        "Meridian Freight Group",
        "meridianfreight.example",
        "logistics",
        960_000,
        "quarterly",
        24,
        7,
        "LW-E18",
        ("Harriet Vance", "Chief Technology Officer"),
        ("Owen Pryce", "Accounts Payable Lead"),
    ),
    (
        "CL-002",
        "Halcyon Health Partners",
        "halcyonhealth.example",
        "healthcare",
        720_000,
        "monthly",
        12,
        4,
        "LW-E18",
        ("Dr. Imogen Hale", "Director of Digital Services"),
        ("Patrick Doyle", "Finance Manager"),
    ),
    (
        "CL-003",
        "Brackenridge Retail",
        "brackenridge.example",
        "retail",
        600_000,
        "monthly",
        12,
        9,
        "LW-E18",
        ("Sanjay Mehta", "Head of E-commerce Platforms"),
        ("Lucy Carver", "Accounts Payable Specialist"),
    ),
    (
        "CL-004",
        "Corvane Energy",
        "corvane.example",
        "energy",
        540_000,
        "quarterly",
        24,
        None,
        "LW-E19",
        ("Fiona Grant", "IT Director"),
        ("Gareth Holt", "Procurement Manager"),
    ),
    (
        "CL-005",
        "Quillon Insurance Services",
        "quillon.example",
        "insurance",
        480_000,
        "monthly",
        12,
        2,
        "LW-E19",
        ("Benjamin Achebe", "Chief Information Officer"),
        ("Tara Quinn", "Finance Controller"),
    ),
    (
        "CL-006",
        "Tamsin Media Labs",
        "tamsinmedia.example",
        "media",
        420_000,
        "monthly",
        12,
        11,
        "LW-E19",
        ("Chloe Fairbairn", "VP Engineering"),
        ("Rory Abbott", "Finance Officer"),
    ),
]

# id, name, kind, domain, monthly fee (None = computed), terms, contact, contact title, cost class
_VENDORS = [
    (
        "VN-01",
        "Vistula Code Works",
        "nearshore",
        "vistulacode.example",
        None,
        30,
        "Marta Zielinska",
        "Delivery Director",
        "opex",
    ),
    (
        "VN-02",
        "Kaveri Tech Services",
        "offshore",
        "kaveritech.example",
        None,
        45,
        "Arjun Nair",
        "Account Director",
        "opex",
    ),
    (
        "VN-03",
        "Nimbus Cloud",
        "cloud",
        "nimbuscloud.example",
        0,
        10,
        "Elliot Shaw",
        "Customer Success Manager",
        "infra",
    ),
    (
        "VN-04",
        "Southbank Workspace",
        "landlord",
        "southbankworkspace.example",
        20_000,
        14,
        "Neil Hargreaves",
        "Property Manager",
        "opex",
    ),
    (
        "VN-05",
        "Stackwise Software",
        "saas",
        "stackwise.example",
        15_000,
        30,
        "Priscilla Moore",
        "Account Executive",
        "opex",
    ),
    (
        "VN-06",
        "Harcourt and Lowe",
        "professional",
        "harcourtlowe.example",
        9_000,
        30,
        "Simon Harcourt",
        "Partner",
        "opex",
    ),
    (
        "VN-07",
        "Brightline Media",
        "marketing",
        "brightlinemedia.example",
        12_000,
        30,
        "Jasmine Cole",
        "Client Services Lead",
        "cac",
    ),
    (
        "VN-08",
        "Thames Merchant Bank",
        "bank",
        "thamesmerchant.example",
        0,
        0,
        "Victoria Lam",
        "Relationship Manager",
        "none",
    ),
]
_NEARSHORE_SHARE = 0.585  # of the subcontractor spend; the rest goes offshore


def default_spec() -> CompanySpec:
    """The MVP company: 20 people, 6 key clients and a cohort of 99, the company_model money."""
    truth = Truth()
    people = []
    for pid, name, title, role, dept, mgr, login, salary, tenure, flags, skills, role_id in _PEOPLE:
        people.append(
            Person(
                id=pid,
                name=name,
                title=title,
                role=role,
                department=dept,
                manager=mgr,
                login=login,
                salary=salary,
                tenure_months=tenure,
                executive=flags == "exec",
                key=flags == "key",
                skills=skills,
                role_id=role_id,
            )
        )
    key_clients = [
        KeyClient(
            id=cid,
            name=name,
            domain=dom,
            industry=ind,
            acv=acv,
            billing=bill,
            term_months=term,
            renewal_in_months=ren,
            account_manager=am,
            contacts=[
                Contact(name=m[0], title=m[1], kind="main"),
                Contact(name=b[0], title=b[1], kind="billing"),
            ],
        )
        for cid, name, dom, ind, acv, bill, term, ren, am, m, b in _KEY_CLIENTS
    ]
    # subcontractors take what the opex budget leaves after payroll and the fixed vendors
    payroll = sum(p.salary for p in people) * (1 + truth.payroll_tax_rate)
    fixed = sum(v[4] for v in _VENDORS if v[4] and v[8] == "opex") * 12
    subcontract = max(truth.opex - payroll - fixed, 0) / 12
    vendors = []
    for vid, name, kind, dom, fee, terms, contact, ctitle, cls in _VENDORS:
        if fee is None:
            share = _NEARSHORE_SHARE if kind == "nearshore" else 1 - _NEARSHORE_SHARE
            fee = round(subcontract * share, -2)
        vendors.append(
            Vendor(
                id=vid,
                name=name,
                kind=kind,
                domain=dom,
                monthly_fee=fee,
                terms_days=terms,
                contact_name=contact,
                contact_title=ctitle,
                cost_class=cls,
            )
        )
    departments = [
        Department(id="management", name="Management", head="LW-E01"),
        Department(id="finance", name="Finance and HR", head="LW-E02"),
        Department(id="delivery", name="Delivery", head="LW-E03"),
        Department(id="engineering", name="Engineering", head="LW-E06"),
        Department(id="support", name="Support", head="LW-E16"),
        Department(id="sales", name="Sales", head="LW-E18"),
        Department(id="marketing", name="Marketing", head="LW-E20"),
    ]
    return CompanySpec(
        departments=departments,
        people=people,
        key_clients=key_clients,
        vendors=vendors,
        truth=truth,
    )
