"""
Airflow DAG: monthly Lending Club pipeline on AWS.

Runs the five Glue steps in order. Each step starts only after the previous one
succeeded, so a failure stops the chain before anything reaches Redshift.

    extract  ->  silver  ->  crawl  ->  check  ->  gold

    extract : loans_extract_job      RDS MySQL -> S3 raw (Parquet)
    silver  : loans_silver_job       S3 raw -> S3 processed (cleaned, typed)
    crawl   : lending_lake_crawler   registers the new month for Athena
    check   : loans_lake_check_job   Athena: raw and processed counts must match
    gold    : loans_gold_job         S3 processed -> Redshift Silver and Gold

Each job finds its own work from the trackers in S3 (_state/), so the DAG only
has to start them in the right order. A month with no new data passes through
every step without loading anything.

AWS access: the Airflow connection 'aws_default' (an IAM user that may only
start and check these four jobs and the crawler). No keys in this file.
"""

from datetime import timedelta

import pendulum
from airflow.providers.amazon.aws.operators.glue import GlueJobOperator
from airflow.providers.amazon.aws.operators.glue_crawler import GlueCrawlerOperator
from airflow.sdk import DAG


# ---------- SETTINGS ----------
AWS_CONN_ID = "aws_default"
REGION = "eu-north-1"

# Settings every task shares
default_args = {
    "owner": "data-engineering",
    "retries": 1,                          # one automatic retry for short AWS hiccups
    "retry_delay": timedelta(minutes=5),
}


# ---------- DAG ----------
with DAG(
    dag_id="lending_monthly_pipeline",
    description="Monthly: RDS -> raw -> processed -> Athena check -> Redshift",
    schedule="0 2 1 * *",                  # 02:00 on the 1st of every month
    start_date=pendulum.datetime(2026, 11, 1, tz="Asia/Kolkata"),
    catchup=False,                         # do not run the months before today
    max_active_runs=1,                     # never two monthly runs at the same time
    default_args=default_args,
    tags=["lending", "glue", "monthly"],
    doc_md=__doc__,
) as dag:

    extract = GlueJobOperator(
        task_id="extract",
        job_name="loans_extract_job",
        aws_conn_id=AWS_CONN_ID,
        region_name=REGION,
        wait_for_completion=True,          # the task only finishes when the Glue job has finished
    )

    silver = GlueJobOperator(
        task_id="silver",
        job_name="loans_silver_job",
        aws_conn_id=AWS_CONN_ID,
        region_name=REGION,
        wait_for_completion=True,
    )

    crawl = GlueCrawlerOperator(
        task_id="crawl",
        config={"Name": "lending_lake_crawler"},
        aws_conn_id=AWS_CONN_ID,
        region_name=REGION,
        wait_for_completion=True,
    )

    check = GlueJobOperator(
        task_id="check",
        job_name="loans_lake_check_job",
        aws_conn_id=AWS_CONN_ID,
        region_name=REGION,
        wait_for_completion=True,
    )

    gold = GlueJobOperator(
        task_id="gold",
        job_name="loans_gold_job",
        aws_conn_id=AWS_CONN_ID,
        region_name=REGION,
        wait_for_completion=True,
    )

    # The order: each step runs only after the previous one succeeded
    extract >> silver >> crawl >> check >> gold