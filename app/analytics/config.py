"""
Single place for every tunable rule of the analytics & accountability system.

Change the numbers here - nothing else in the backend hardcodes a weight,
an SLA, or a threshold. The offline demo engine in the prototype
(prototype-v2/analytics/engine.js -> CONFIG) mirrors these values, and
tests/test_parity.py fails if the two ever drift apart.
"""

# ---------------------------------------------------------------------------
# Administrative levels, lowest to highest. Urban wards escalate straight from
# the Ward Councillor to the SDM (the Village Head sits on rural chains only,
# matching the note in backend/README.md that chains vary by area type).
# ---------------------------------------------------------------------------
LEVELS = ["WARD", "SDM", "DISTRICT", "STATE", "COUNTRY"]
LEVEL_RANK = {"WARD": 1, "REGION": 1.5, "SDM": 2, "DISTRICT": 3, "STATE": 4, "COUNTRY": 5}
ESCALATION_CHAIN = ["WARD", "SDM", "DISTRICT", "STATE", "COUNTRY"]

UNIT_TYPE_LEVEL = {
    "ward": "WARD",
    "region": "REGION",
    "sdm": "SDM",
    "district": "DISTRICT",
    "state": "STATE",
    "country": "COUNTRY",
}
LEVEL_UNIT_TYPE = {v: k for k, v in UNIT_TYPE_LEVEL.items()}

# Existing role enum (backend/migrations/001_schema.sql -> app_role)
LEVEL_ROLE = {
    "WARD": "WARD_COUNCILLOR",
    "REGION": "VILLAGE_HEAD",
    "SDM": "TEHSILDAR_SDM",
    "DISTRICT": "DM_COLLECTOR",
    "STATE": "CM",
    "COUNTRY": "PM_CENTRAL_ADMIN",
}
ROLE_LABEL = {
    "WARD_COUNCILLOR": "Ward Councillor",
    "VILLAGE_HEAD": "Village Head",
    "TEHSILDAR_SDM": "Tehsildar / SDM",
    "DM_COLLECTOR": "DM / Collector",
    "CM": "Chief Minister",
    "PM_CENTRAL_ADMIN": "PM / Central Government",
}
# Plural nouns used in ranking context ("Rank #3 of 6 Ward Councillors in ...")
LEVEL_PEER_NOUN = {
    "WARD": ("Ward Councillor", "Ward Councillors"),
    "SDM": ("SDM", "SDMs"),
    "DISTRICT": ("DM", "DMs"),
    "STATE": ("CM", "CMs"),
}

# ---------------------------------------------------------------------------
# SLA per category, in days - taken from backend/seed/001_seed_data.sql
# (escalation_rules). Each level of the chain gets the same window again when
# a complaint is escalated, exactly like functions/escalation.sql does.
# ---------------------------------------------------------------------------
CATEGORY_SLA_DAYS = {
    "crime": 3,
    "publicsafety": 5,
    "water": 7,
    "electricity": 7,
    "garbage": 7,
    "sanitation": 10,
    "drainage": 10,
    "community": 14,
    "infrastructure": 14,
    "environment": 14,
    "roads": 14,
    "streetlights": 14,
    "traffic": 14,
    "other": 14,
}
DEFAULT_SLA_DAYS = 14

# ---------------------------------------------------------------------------
# Performance scoring model (section 15). Weights must sum to 1.0.
# ---------------------------------------------------------------------------
SCORING = {
    "weights": {
        "resolution": 0.40,  # verified resolutions / complaints received
        "sla": 0.30,         # resolved within SLA / complaints whose SLA outcome is known
        "speed": 0.20,       # how much of the SLA window was left when resolved
        "escalation": 0.10,  # 100 - escalation rate
    },
    # Anti-gaming (section 16): points deducted at a 100% citizen-dispute rate.
    "reopen_penalty": 40.0,
    # Below these, show "Insufficient data" instead of a misleading 0% / 100%.
    "min_received": 3,
    "min_due": 2,           # complaints resolved or past SLA (see engine.finalize)
    # SDM/DM/CM are judged on the escalated cases they personally handled
    # ("own") and on the health of the whole jurisdiction ("system").
    "level_blend": {
        "SDM": {"own": 0.5, "system": 0.5},
        "DISTRICT": {"own": 0.4, "system": 0.6},
        "STATE": {"own": 0.3, "system": 0.7},
        "COUNTRY": {"own": 0.0, "system": 1.0},
    },
    "min_own_received": 2,
    # Display bands for maps and badges.
    "bands": {"good": 80.0, "watch": 60.0},
    "sla_target": 85.0,
}

# A resolution only counts towards performance if it carries photo evidence
# or the citizen confirmed it. A closure the citizen disputes reopens the
# complaint at the same level and counts against the authority.
RESOLUTION_POLICY = {
    "requires_evidence_or_confirmation": True,
    "confirmation_window_days": 7,
}

# Timezone for calendar periods ("this month", "today"): IST, fixed offset.
TZ_OFFSET_MINUTES = 330
AT_RISK_HOURS = 48
ESCALATION_JOB_INTERVAL_SECONDS = 30
