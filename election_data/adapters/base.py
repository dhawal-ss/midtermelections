"""Adapter interface and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator

from election_data.ingestion.results import ResultObservation

ADAPTERS: dict[str, type["StateResultsAdapter"]] = {}


def register(name: str):
    def wrap(cls: type["StateResultsAdapter"]) -> type["StateResultsAdapter"]:
        ADAPTERS[name] = cls
        cls.name = name
        return cls
    return wrap


class AdapterNotImplemented(NotImplementedError):
    pass


class StateResultsAdapter(ABC):
    name = "base"

    def __init__(self, state_code: str, options: dict[str, Any] | None = None):
        self.state_code = state_code
        self.options = options or {}

    @abstractmethod
    def observations(self) -> Iterator[tuple[ResultObservation, dict[str, Any]]]:
        """Yield (observation, original_row) pairs; original_row is stored as raw evidence."""


def get_adapter(state: dict[str, Any], options: dict[str, Any] | None = None) -> StateResultsAdapter:
    name = state.get("results_adapter")
    if not name or state.get("adapter_status") == "not_implemented":
        raise AdapterNotImplemented(
            f"{state['code']}: no results adapter implemented (adapter_status="
            f"{state.get('adapter_status')}). Use the generic CSV adapter with an official export: "
            f"python -m scripts.ingest_all --only state_results --state {state['code']} --file <csv> "
            f"--mapping <yaml>"
        )
    if name not in ADAPTERS:
        raise AdapterNotImplemented(f"{state['code']}: adapter '{name}' is configured but not registered")
    return ADAPTERS[name](state["code"], {**(state.get("adapter_options") or {}), **(options or {})})
