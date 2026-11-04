"""APScheduler runner driven by `schedules` in config/sources.yaml."""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from election_data.http import configure_logging
from election_data import config
from election_data.jobs.registry import JOBS

log = logging.getLogger(__name__)


def build_trigger(spec: dict):
    if "cron" in spec:
        return CronTrigger.from_crontab(spec["cron"], timezone="UTC")
    kwargs = {}
    for key, unit in (("every_minutes", "minutes"), ("every_hours", "hours"), ("every_days", "days")):
        if key in spec:
            kwargs[unit] = spec[key]
    if not kwargs:
        raise ValueError(f"Schedule needs cron or every_*: {spec}")
    start = datetime.fromisoformat(spec["active_from"]) if spec.get("active_from") else None
    end = datetime.fromisoformat(spec["active_to"]) if spec.get("active_to") else None
    return IntervalTrigger(start_date=start, end_date=end, timezone="UTC", **kwargs)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1,
                                                                 "misfire_grace_time": 600})
    for name, spec in config.load_sources().get("schedules", {}).items():
        job = JOBS.get(name)
        if job is None:
            log.warning("No job registered for schedule '%s' (yet); skipping", name)
            continue
        scheduler.add_job(job, build_trigger(spec), id=name, name=name)
    return scheduler


def main() -> None:
    configure_logging(config.LOG_LEVEL)
    scheduler = build_scheduler()
    for job in scheduler.get_jobs():
        log.info("Scheduled %s: %s", job.id, job.trigger)
    scheduler.start()


if __name__ == "__main__":
    main()
