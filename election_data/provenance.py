"""Source provenance: source registration, ingestion runs, immutable raw records.

Ingestion pattern used by every module:
  1. request data from the provider
  2. store the original payload in raw_records (content-hashed, deduplicated)
  3. record retrieval time and redacted source URL
  4. validate, normalize, and upsert into domain tables
  5. on failure, mark the run failed without touching previously stored data
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from election_data import config
from election_data.http import redact_text, redact_url

log = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def ensure_source(conn: psycopg.Connection, source_key: str, **overrides: Any) -> int:
    """Upsert a data_sources row from config/sources.yaml (or overrides)."""
    if source_key.startswith("state:"):
        code = source_key.split(":", 1)[1]
        state = next(s for s in config.state_registry() if s["code"] == code)
        cfg = {
            "name": state["office"],
            "base_url": state.get("official_election_url"),
            "source_type": "web",
            "authority_level": "official_election_authority",
            "access_type": "public",
        }
    else:
        cfg = dict(config.source_config(source_key))
    cfg.update(overrides)
    row = conn.execute(
        """
        INSERT INTO data_sources (source_key, name, base_url, source_type, authority_level,
                                  access_type, terms_url, attribution, license_notes, enabled)
        VALUES (%(key)s, %(name)s, %(base_url)s, %(source_type)s, %(authority_level)s,
                %(access_type)s, %(terms_url)s, %(attribution)s, %(license_notes)s, %(enabled)s)
        ON CONFLICT (source_key) DO UPDATE SET
            name = EXCLUDED.name, base_url = EXCLUDED.base_url,
            source_type = EXCLUDED.source_type, authority_level = EXCLUDED.authority_level,
            access_type = EXCLUDED.access_type, terms_url = EXCLUDED.terms_url,
            attribution = EXCLUDED.attribution, license_notes = EXCLUDED.license_notes,
            enabled = EXCLUDED.enabled
        RETURNING id
        """,
        {
            "key": source_key,
            "name": cfg["name"],
            "base_url": cfg.get("base_url"),
            "source_type": cfg.get("source_type", "api"),
            "authority_level": cfg["authority_level"],
            "access_type": cfg.get("access_type"),
            "terms_url": cfg.get("terms_url"),
            "attribution": cfg.get("attribution"),
            "license_notes": cfg.get("license_notes"),
            "enabled": cfg.get("enabled", True),
        },
    ).fetchone()
    return row["id"]


def register_all_sources(conn: psycopg.Connection) -> int:
    count = 0
    for key in config.load_sources()["sources"]:
        ensure_source(conn, key)
        count += 1
    for state in config.state_registry():
        ensure_source(conn, f"state:{state['code']}")
        count += 1
    return count


# ---------------------------------------------------------------------------
# Ingestion runs
# ---------------------------------------------------------------------------
@dataclass
class RawResult:
    id: int
    is_new: bool


@dataclass
class IngestionRun:
    """Context manager that records an ingestion_runs row.

    The run row is written on its own autocommit connection so that a failed
    data transaction still leaves a durable 'failed' run record.
    """

    conn: psycopg.Connection
    source_key: str
    job_name: str
    parameters: dict[str, Any] = field(default_factory=dict)
    received: int = 0
    inserted: int = 0
    unchanged: int = 0
    failed: int = 0
    id: str | None = None
    source_id: int | None = None
    errors: list[str] = field(default_factory=list)

    def __enter__(self) -> "IngestionRun":
        self.source_id = ensure_source(self.conn, self.source_key)
        self.conn.commit()
        row = self.conn.execute(
            "INSERT INTO ingestion_runs (source_id, job_name, parameters) VALUES (%s, %s, %s) RETURNING id",
            (self.source_id, self.job_name, Jsonb(self.parameters)),
        ).fetchone()
        self.conn.commit()
        self.id = str(row["id"])
        log.info("Run %s started: %s (%s)", self.id, self.job_name, self.source_key)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.conn.rollback()
            status, message = "failed", redact_text(f"{exc_type.__name__}: {exc}")
        else:
            self.conn.commit()
            status = "partial" if self.failed else "succeeded"
            message = redact_text("; ".join(self.errors[:20])) or None
        self.conn.execute(
            """
            UPDATE ingestion_runs SET finished_at = NOW(), status = %s, records_received = %s,
                   records_inserted = %s, records_unchanged = %s, records_failed = %s, error_message = %s
            WHERE id = %s
            """,
            (status, self.received, self.inserted, self.unchanged, self.failed, message, self.id),
        )
        self.conn.commit()
        log.info(
            "Run %s %s: received=%d inserted=%d unchanged=%d failed=%d",
            self.id, status, self.received, self.inserted, self.unchanged, self.failed,
        )
        return False  # never swallow exceptions

    def record_failure(self, message: str) -> None:
        self.failed += 1
        message = redact_text(message) or ""
        self.errors.append(message)
        log.warning("Run %s record failure: %s", self.id, message)

    # -- raw evidence -------------------------------------------------------
    def store_raw(
        self,
        record_type: str,
        payload: Any,
        external_id: str | None = None,
        source_url: str | None = None,
        source_updated_at: datetime | str | None = None,
    ) -> RawResult:
        """Insert the original payload; identical content is stored once."""
        self.received += 1
        digest = content_hash(payload)
        row = self.conn.execute(
            """
            INSERT INTO raw_records (source_id, run_id, external_id, record_type, payload,
                                     content_hash, source_url, source_updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, record_type, content_hash) DO NOTHING
            RETURNING id
            """,
            (
                self.source_id, self.id, external_id, record_type,
                Jsonb(json.loads(canonical_json(payload))), digest,
                redact_url(source_url) if source_url else None, source_updated_at,
            ),
        ).fetchone()
        if row:
            self.inserted += 1
            return RawResult(row["id"], True)
        existing = self.conn.execute(
            "SELECT id FROM raw_records WHERE source_id = %s AND record_type = %s AND content_hash = %s",
            (self.source_id, record_type, digest),
        ).fetchone()
        self.unchanged += 1
        return RawResult(existing["id"], False)

    def archive_file(self, source_url: str, content: bytes, filename: str, content_type: str | None = None) -> Path:
        """Write a downloaded file to data/raw/<source>/<date>/ and register its checksum."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        directory = config.RAW_DIR / self.source_key.replace(":", "_") / day
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        path = directory / f"{digest[:12]}_{filename}"
        if not path.exists():
            path.write_bytes(content)
        self.conn.execute(
            """
            INSERT INTO raw_files (source_id, run_id, source_url, local_path, sha256, byte_size, content_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, sha256) DO NOTHING
            """,
            (self.source_id, self.id, redact_url(source_url), str(path), digest, len(content), content_type),
        )
        return path


# ---------------------------------------------------------------------------
# Schema-change detection, verification, discrepancies
# ---------------------------------------------------------------------------
def record_schema_fingerprint(
    conn: psycopg.Connection, source_id: int, dataset: str, columns: Iterable[str]
) -> dict[str, Any]:
    """Store the column set of a feed; report whether it differs from the last one seen."""
    cols = sorted(str(c) for c in columns)
    fingerprint = hashlib.sha256("|".join(cols).encode()).hexdigest()
    previous = conn.execute(
        """SELECT columns, fingerprint FROM schema_fingerprints
           WHERE source_id = %s AND dataset = %s ORDER BY last_seen_at DESC LIMIT 1""",
        (source_id, dataset),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO schema_fingerprints (source_id, dataset, columns, fingerprint)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (source_id, dataset, fingerprint) DO UPDATE SET last_seen_at = NOW()
        """,
        (source_id, dataset, cols, fingerprint),
    )
    changed = previous is not None and previous["fingerprint"] != fingerprint
    result = {"changed": changed, "fingerprint": fingerprint}
    if changed:
        old = set(previous["columns"])
        result["added"] = sorted(set(cols) - old)
        result["removed"] = sorted(old - set(cols))
        log.warning("Schema change in %s/%s: %s", source_id, dataset, result)
    return result


def record_verification(
    conn: psycopg.Connection,
    verification_type: str,
    status: str,
    details: dict[str, Any] | None = None,
    source_record_id: int | None = None,
    entity_table: str | None = None,
    entity_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO source_verifications (source_record_id, entity_table, entity_id,
                                          verification_type, verification_status, details)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (source_record_id, entity_table, entity_id, verification_type, status, Jsonb(details or {})),
    )


def record_discrepancy(
    conn: psycopg.Connection,
    entity_table: str,
    entity_id: int | None,
    field_name: str,
    discrepancy_type: str,
    values_by_source: dict[str, Any],
    severity: str = "warning",
) -> None:
    """Preserve a conflict between sources instead of silently choosing one."""
    key = content_hash([entity_table, entity_id, field_name, discrepancy_type])
    conn.execute(
        """
        INSERT INTO data_discrepancies (discrepancy_key, entity_table, entity_id, field_name,
                                        discrepancy_type, values_by_source, severity)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (discrepancy_key) DO UPDATE SET
            values_by_source = EXCLUDED.values_by_source,
            severity = EXCLUDED.severity,
            last_detected_at = NOW(),
            status = CASE WHEN data_discrepancies.status = 'resolved' THEN 'open'
                          ELSE data_discrepancies.status END
        """,
        (key, entity_table, entity_id, field_name, discrepancy_type,
         Jsonb(json.loads(canonical_json(values_by_source))), severity),
    )
