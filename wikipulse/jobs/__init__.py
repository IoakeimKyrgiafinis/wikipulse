"""Scheduled batch jobs (stage 6, spec 06c).

Each job is a plain command (`python -m wikipulse.jobs.<job> <step> --date YYYY-MM-DD`)
that works for any past day and can be re-run safely. Airflow (`dags/`) only decides
*when* each step runs, in which order, and what happens on failure; all the work is
here, so a job also runs by hand without Airflow.
"""
