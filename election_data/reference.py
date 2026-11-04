"""Static reference data seeded by the project.

These values are stable public facts (FIPS codes, 2020 apportionment) or the
2026 seat map. They are stored with source 'project_reference' and
verification_status 'unverified' until corroborated by FEC and state data
(see jobs/validation.py: check_reference_coverage).
"""
from __future__ import annotations

# code: (name, FIPS, House seats under the 2020 apportionment)
STATES: dict[str, tuple[str, str, int]] = {
    "AL": ("Alabama", "01", 7), "AK": ("Alaska", "02", 1), "AZ": ("Arizona", "04", 9),
    "AR": ("Arkansas", "05", 4), "CA": ("California", "06", 52), "CO": ("Colorado", "08", 8),
    "CT": ("Connecticut", "09", 5), "DE": ("Delaware", "10", 1), "DC": ("District of Columbia", "11", 0),
    "FL": ("Florida", "12", 28), "GA": ("Georgia", "13", 14), "HI": ("Hawaii", "15", 2),
    "ID": ("Idaho", "16", 2), "IL": ("Illinois", "17", 17), "IN": ("Indiana", "18", 9),
    "IA": ("Iowa", "19", 4), "KS": ("Kansas", "20", 4), "KY": ("Kentucky", "21", 6),
    "LA": ("Louisiana", "22", 6), "ME": ("Maine", "23", 2), "MD": ("Maryland", "24", 8),
    "MA": ("Massachusetts", "25", 9), "MI": ("Michigan", "26", 13), "MN": ("Minnesota", "27", 8),
    "MS": ("Mississippi", "28", 4), "MO": ("Missouri", "29", 8), "MT": ("Montana", "30", 2),
    "NE": ("Nebraska", "31", 3), "NV": ("Nevada", "32", 4), "NH": ("New Hampshire", "33", 2),
    "NJ": ("New Jersey", "34", 12), "NM": ("New Mexico", "35", 3), "NY": ("New York", "36", 26),
    "NC": ("North Carolina", "37", 14), "ND": ("North Dakota", "38", 1), "OH": ("Ohio", "39", 15),
    "OK": ("Oklahoma", "40", 5), "OR": ("Oregon", "41", 6), "PA": ("Pennsylvania", "42", 17),
    "RI": ("Rhode Island", "44", 2), "SC": ("South Carolina", "45", 7), "SD": ("South Dakota", "46", 1),
    "TN": ("Tennessee", "47", 9), "TX": ("Texas", "48", 38), "UT": ("Utah", "49", 4),
    "VT": ("Vermont", "50", 1), "VA": ("Virginia", "51", 11), "WA": ("Washington", "53", 10),
    "WV": ("West Virginia", "54", 2), "WI": ("Wisconsin", "55", 8), "WY": ("Wyoming", "56", 1),
}

FIPS_TO_STATE = {fips: code for code, (_, fips, _) in STATES.items()}

# Class II seats up for regular election in 2026
SENATE_2026_REGULAR = [
    "AL", "AK", "AR", "CO", "DE", "GA", "ID", "IL", "IA", "KS", "KY", "LA", "ME", "MA", "MI",
    "MN", "MS", "MT", "NE", "NH", "NJ", "NM", "NC", "OK", "OR", "RI", "SC", "SD", "TN", "TX",
    "VA", "WV", "WY",
]
# Special elections for the remainder of terms vacated in 2025 (Class III seats)
SENATE_2026_SPECIAL = ["FL", "OH"]

GOVERNOR_2026 = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL", "IA", "KS", "ME",
    "MD", "MA", "MI", "MN", "NE", "NV", "NH", "NM", "NY", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "VT", "WI", "WY",
]

# FEC party codes (subset; unknown codes are inserted on first sight)
PARTIES = {
    "DEM": "Democratic Party", "REP": "Republican Party", "LIB": "Libertarian Party",
    "GRE": "Green Party", "IND": "Independent", "NPA": "No Party Affiliation",
    "CON": "Constitution Party", "DFL": "Democratic-Farmer-Labor", "WFP": "Working Families Party",
    "NNE": "None", "UNK": "Unknown", "OTH": "Other", "W": "Write-In",
}


def house_district_numbers(state_code: str) -> list[str]:
    """District numbers as FEC/Census write them: '00' for at-large/delegate seats."""
    seats = STATES[state_code][2]
    if seats <= 1:
        return ["00"]
    return [f"{n:02d}" for n in range(1, seats + 1)]


def contest_key(year: int, stage: str, office_code: str, special: bool = False, party: str | None = None) -> str:
    parts = [str(year), stage, office_code]
    if special:
        parts.append("SPECIAL")
    if party:
        parts.append(party)
    return ":".join(parts)


def house_office_code(state: str, district: str) -> str:
    return f"US-HOUSE-{state}-{district}"


def senate_seat_class(state: str) -> str | None:
    """Senate class contested in 2026 for a state (no state has both in 2026)."""
    if state in SENATE_2026_SPECIAL:
        return "III"
    if state in SENATE_2026_REGULAR:
        return "II"
    return None


def senate_office_code(state: str) -> str:
    return f"US-SENATE-{state}-{senate_seat_class(state) or 'UNKNOWN'}"


def governor_office_code(state: str) -> str:
    return f"GOV-{state}"
