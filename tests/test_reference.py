"""Reference data and the 51-jurisdiction source registry."""
from election_data import config, reference
from election_data.ingestion.candidates import parse_fec_name
from election_data.jobs.scheduler import build_trigger

URL_KEYS = {"code", "name", "office", "official_election_url", "results_url", "candidates_url",
            "voter_lookup_url", "results_adapter", "adapter_status"}


def test_apportionment_totals_435():
    assert sum(seats for _, _, seats in reference.STATES.values()) == 435
    assert len(reference.STATES) == 51


def test_2026_seat_map():
    assert len(reference.SENATE_2026_REGULAR) == 33
    assert set(reference.SENATE_2026_SPECIAL) == {"FL", "OH"}
    assert not set(reference.SENATE_2026_REGULAR) & set(reference.SENATE_2026_SPECIAL)
    assert len(reference.GOVERNOR_2026) == 36


def test_at_large_district_numbers():
    assert reference.house_district_numbers("WY") == ["00"]
    assert reference.house_district_numbers("DC") == ["00"]
    assert reference.house_district_numbers("TX")[-1] == "38"


def test_registry_covers_50_states_and_dc():
    states = config.state_registry()
    codes = [s["code"] for s in states]
    assert len(codes) == 51 and set(codes) == set(reference.STATES)
    for s in states:
        assert URL_KEYS <= set(s), s["code"]
        assert s["official_election_url"].startswith("https://"), s["code"]
        # honest status: nothing claims a working adapter without code behind it
        assert s["adapter_status"] in ("not_implemented", "manual_csv", "configured", "tested", "production")


def test_sources_have_valid_authority_levels():
    allowed = {"official_election_authority", "official_federal", "academic", "third_party",
               "commercial", "project_reference"}
    for key, src in config.load_sources()["sources"].items():
        assert src["authority_level"] in allowed, key


def test_schedules_build_triggers():
    for name, spec in config.load_sources()["schedules"].items():
        assert build_trigger(spec) is not None, name


def test_parse_fec_name():
    assert parse_fec_name("DOE, JANE A. MS.") == ("Jane A. Doe", "Jane", "Doe")
    assert parse_fec_name("SINGLE NAME") == ("Single Name", None, None)
