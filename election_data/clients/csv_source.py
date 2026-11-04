"""Generic tabular loader for EAC, MIT Election Lab, state offices and other downloads."""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

_HTML_SIGNATURES = (b"<!doctype html", b"<html")


def normalize_column(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower())
    return name.strip("_")


def looks_like_html(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return head.startswith(_HTML_SIGNATURES)


def read_table(source: Path | str | bytes, sep: str | None = None) -> pd.DataFrame:
    """Read CSV/TSV as strings, with normalized snake_case column names.

    Everything is read as text so identifiers like FIPS '01' or district '00'
    keep their leading zeros; typed conversion happens during normalization.
    """
    if isinstance(source, bytes):
        if looks_like_html(source):
            raise ValueError("Received an HTML page where a data file was expected")
        buffer: Any = io.BytesIO(source)
    else:
        buffer = source
    df = pd.read_csv(buffer, dtype=str, sep=sep, engine="python" if sep is None else "c",
                     keep_default_na=False, na_values=[""], encoding_errors="replace")
    df.columns = [normalize_column(c) for c in df.columns]
    return df


def load_csv(path: Path | str) -> pd.DataFrame:
    return read_table(Path(path))


def iter_records(df: pd.DataFrame) -> Iterator[dict[str, Any]]:
    """Yield rows as dicts with NaN converted to None."""
    for row in df.to_dict(orient="records"):
        yield {k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in row.items()}


def fingerprint_row(row: dict[str, Any] | pd.Series) -> str:
    data = row.to_dict() if isinstance(row, pd.Series) else row
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def check_required_columns(columns: Iterable[str], required: Iterable[str]) -> list[str]:
    present = set(columns)
    return [c for c in required if c not in present]
