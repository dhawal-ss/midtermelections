"""State results adapters.

Each of the 51 jurisdictions publishes results differently (vendor ENR
sites, CSV/XML exports, PDFs). An adapter turns one jurisdiction's format
into ResultObservation objects. The registry below lists what is actually
implemented; config/sources.yaml records each state's adapter_status.
"""
from election_data.adapters.base import ADAPTERS, StateResultsAdapter, get_adapter  # noqa: F401
from election_data.adapters import generic_csv  # noqa: F401  (registers itself)
