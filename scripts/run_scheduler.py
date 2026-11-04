"""Start the APScheduler loop (see `schedules` in config/sources.yaml)."""
from election_data.jobs.scheduler import main

if __name__ == "__main__":
    main()
