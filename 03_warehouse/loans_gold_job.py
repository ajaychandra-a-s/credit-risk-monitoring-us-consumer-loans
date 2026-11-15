"""
Glue Python shell job: Silver (S3) -> Redshift Silver -> Redshift Gold, incremental.

Only new months are loaded. Existing rows and existing dimension IDs are never changed.

How it works:
1. Read the last month Silver processed       (_state/silver_tracker.txt).
2. Read the last month this job loaded         (_state/gold_tracker.txt).
3. For every month in between, in ONE transaction (all steps succeed, or none are kept):
   a. silver.loans   : delete the month, then COPY it from processed/issue_month=YYYY-MM/
   b. dimensions     : add only values not seen before (Redshift creates the IDs)
   c. gold.fact_loan : delete the month, then insert it with the dimension IDs
   d. gold.agg_vintage: delete the month, then recalculate it
4. Check the month: fact rows = Silver rows, no missing dimension IDs,
   aggregate total = fact rows.
5. Move the Gold tracker forward after each month passes its checks.
If there are no new months, nothing is loaded.

Deleting a month before inserting it makes every step safe to re-run:
a month is never loaded twice.

SQL is sent with the Redshift Data API, so the job needs no database driver,
no VPC connection and no password in the code. It logs in with the Redshift
admin secret stored in AWS Secrets Manager.

Job details in Glue:
- Type: Python shell, Python 3.9, 'Load common analytics libraries' ticked, 1/16 DPU
- IAM role: the Glue job role, with S3 access, redshift-data permissions and
  secretsmanager:GetSecretValue
- Timeout: 15 minutes
- No connection
- Job parameters: --workgroup_name, --database, --secret_arn

Before the first run:
- The Gold tables must exist (built once with sql/redshift/02 to 04),
  with IDENTITY ID columns in the four ID dimensions
- _state/gold_tracker.txt must exist and hold the last month already in Redshift
"""

# ---------- IMPORTS ----------
import sys
import time

import boto3
from awsglue.utils import getResolvedOptions


# ---------- SETTINGS ----------
BUCKET = "lending-data-de-project"
PROCESSED_PREFIX = "lending-data-monthly/processed/"
SILVER_TRACKER_KEY = "lending-data-monthly/_state/silver_tracker.txt"
GOLD_TRACKER_KEY = "lending-data-monthly/_state/gold_tracker.txt"

POLL_SECONDS = 3   # wait between "is the SQL finished yet?" checks

# No keys: in Glue, boto3 uses the job's IAM role
s3 = boto3.client("s3")
redshift = boto3.client("redshift-data")


# ---------- TRACKERS ----------
def read_tracker(key):
    """Return the month stored in a tracker file, e.g. '2018-11'."""
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8").strip()


def update_tracker(key, month):
    s3.put_object(Bucket=BUCKET, Key=key, Body=month.encode("utf-8"))
    print(f"[update_tracker] {key} set to {month}")


# ---------- MONTHS ----------
def next_month(month):
    """'2018-11' -> '2018-12', '2018-12' -> '2019-01'."""
    year, mon = map(int, month.split("-"))
    if mon == 12:
        return f"{year + 1}-01"
    return f"{year}-{mon + 1:02d}"


def months_to_load(last_loaded, last_processed):
    """Every month after last_loaded, up to and including last_processed."""
    months = []
    month = next_month(last_loaded)
    while month <= last_processed:
        months.append(month)
        month = next_month(month)
    return months


# ---------- MONTH SQL ----------
def silver_sql(month):
    """Replace one month in silver.loans with the files from processed/."""
    path = f"s3://{BUCKET}/{PROCESSED_PREFIX}issue_month={month}/"
    return [
        f"DELETE FROM silver.loans WHERE issue_month = '{month}'",
        f"COPY silver.loans FROM '{path}' IAM_ROLE DEFAULT FORMAT AS PARQUET",
    ]


def dimension_sql(month):
    """Add values seen for the first time this month. Existing rows are never changed.

    Each statement reads: insert this month's distinct values that are NOT IN the table yet.
    The ID columns are IDENTITY columns, so Redshift creates the new IDs itself.
    """
    loans = f"silver.loans WHERE issue_month = '{month}'"
    return [
        # sub_grade is enough to check: B2 always belongs to grade B
        f"""INSERT INTO gold.dim_grade (grade, sub_grade)
        SELECT DISTINCT grade, sub_grade
        FROM {loans}
          AND sub_grade NOT IN (SELECT sub_grade FROM gold.dim_grade)""",

        f"""INSERT INTO gold.dim_purpose (purpose)
        SELECT DISTINCT purpose
        FROM {loans}
          AND purpose NOT IN (SELECT purpose FROM gold.dim_purpose)""",

        f"""INSERT INTO gold.dim_geography (state)
        SELECT DISTINCT state
        FROM {loans}
          AND state NOT IN (SELECT state FROM gold.dim_geography)""",

        f"""INSERT INTO gold.dim_loan_status (loan_status, loan_status_category, is_bad)
        SELECT DISTINCT loan_status, loan_status_category, is_bad
        FROM {loans}
          AND loan_status NOT IN (SELECT loan_status FROM gold.dim_loan_status)""",

        # dim_date has no ID: the month itself is the key
        f"""INSERT INTO gold.dim_date (month_start, year, quarter, month_num, month_name, year_month)
        SELECT month_start,
               EXTRACT(year FROM month_start),
               EXTRACT(quarter FROM month_start),
               EXTRACT(month FROM month_start),
               TO_CHAR(month_start, 'Mon'),
               TO_CHAR(month_start, 'YYYY-MM')
        FROM (
            SELECT DISTINCT issue_date::DATE AS month_start FROM {loans}
            UNION
            SELECT DISTINCT last_payment_date::DATE AS month_start FROM {loans}
        ) AS months
        WHERE month_start IS NOT NULL
          AND month_start NOT IN (SELECT month_start FROM gold.dim_date)""",
    ]


def fact_sql(month):
    """Replace one month in gold.fact_loan, looking up the dimension IDs."""
    start, end = f"{month}-01", f"{next_month(month)}-01"
    return [
        f"""DELETE FROM gold.fact_loan
        WHERE issue_date >= DATE '{start}' AND issue_date < DATE '{end}'""",

        f"""INSERT INTO gold.fact_loan (
            loan_key, grade_id, purpose_id, geography_id, status_id,
            issue_date, last_payment_date, term_months, loan_amount, installment, interest_rate,
            annual_income, employment_length_years, home_ownership, verification_status, dti,
            revolving_balance, revolving_utilization,
            outstanding_principal, total_payment, principal_received, interest_received, recoveries,
            is_bad, flag_zero_income, flag_revol_util_over_100
        )
        SELECT l.loan_key,
               grd.grade_id,
               p.purpose_id,
               geo.geography_id,
               st.status_id,
               l.issue_date::DATE,
               l.last_payment_date::DATE,
               l.term_months,
               l.loan_amount,
               l.installment,
               l.interest_rate,
               l.annual_income,
               l.employment_length_years,
               l.home_ownership,
               l.verification_status,
               l.dti,
               l.revolving_balance,
               l.revolving_utilization,
               l.outstanding_principal,
               l.total_payment,
               l.principal_received,
               l.interest_received,
               l.recoveries,
               l.is_bad,
               l.flag_zero_income,
               l.flag_revol_util_over_100
        FROM silver.loans AS l
        LEFT JOIN gold.dim_grade       AS grd ON l.grade = grd.grade AND l.sub_grade = grd.sub_grade
        LEFT JOIN gold.dim_purpose     AS p   ON l.purpose = p.purpose
        LEFT JOIN gold.dim_geography   AS geo ON l.state = geo.state
        LEFT JOIN gold.dim_loan_status AS st  ON l.loan_status = st.loan_status
        WHERE l.issue_month = '{month}'""",
    ]


def aggregate_sql(month):
    """Recalculate one month of gold.agg_vintage from the fact table."""
    start, end = f"{month}-01", f"{next_month(month)}-01"
    return [
        f"DELETE FROM gold.agg_vintage WHERE issue_month = '{month}'",

        f"""INSERT INTO gold.agg_vintage (
            issue_month, grade, loans, funded_amount, bad_loans, bad_rate_pct, avg_interest_rate
        )
        SELECT TO_CHAR(f.issue_date, 'YYYY-MM'),
               g.grade,
               COUNT(*),
               CAST(SUM(f.loan_amount) AS DECIMAL(15,2)),
               SUM(f.is_bad),
               CAST(AVG(f.is_bad::FLOAT) * 100 AS DECIMAL(5,2)),
               CAST(AVG(f.interest_rate) AS DECIMAL(5,2))
        FROM gold.fact_loan AS f
        JOIN gold.dim_grade AS g ON f.grade_id = g.grade_id
        WHERE f.issue_date >= DATE '{start}' AND f.issue_date < DATE '{end}'
        GROUP BY 1, 2""",
    ]


# ---------- REDSHIFT DATA API ----------
def wait_for(statement_id):
    """Wait until a Data API statement finishes. Stop the job with the error if it fails."""
    while True:
        desc = redshift.describe_statement(Id=statement_id)
        status = desc["Status"]
        if status == "FINISHED":
            return desc
        if status in ("FAILED", "ABORTED"):
            raise RuntimeError(f"Redshift statement {status}: {desc.get('Error')}")
        time.sleep(POLL_SECONDS)


def run_batch(conn, sqls, label):
    """Run several statements as one transaction: all succeed or none are applied."""
    start = time.time()
    resp = redshift.batch_execute_statement(Sqls=sqls, **conn)
    wait_for(resp["Id"])
    print(f"[run_batch] {label}: {len(sqls)} statements OK in {time.time() - start:.0f}s")


def query_value(conn, sql):
    """Run one SELECT that returns a single number, and return it."""
    resp = redshift.execute_statement(Sql=sql, **conn)
    wait_for(resp["Id"])
    result = redshift.get_statement_result(Id=resp["Id"])
    value = result["Records"][0][0]
    return value.get("longValue", 0)   # an empty SUM comes back as null, treat it as 0


# ---------- STEPS ----------
def load_month(conn, month):
    """Load one month into Silver, the dimensions, the fact and the aggregate, in one transaction."""
    sqls = silver_sql(month) + dimension_sql(month) + fact_sql(month) + aggregate_sql(month)
    run_batch(conn, sqls, f"month {month}")


def check_month(conn, month):
    """Stop the job if the month did not arrive complete in every table."""
    start, end = f"{month}-01", f"{next_month(month)}-01"
    in_month = f"issue_date >= DATE '{start}' AND issue_date < DATE '{end}'"

    silver_rows = query_value(conn, f"SELECT COUNT(*) FROM silver.loans WHERE issue_month = '{month}'")
    fact_rows = query_value(conn, f"SELECT COUNT(*) FROM gold.fact_loan WHERE {in_month}")
    missing_ids = query_value(conn, f"""
        SELECT COUNT(*) FROM gold.fact_loan
        WHERE {in_month}
          AND (grade_id IS NULL OR purpose_id IS NULL OR geography_id IS NULL OR status_id IS NULL)""")
    agg_loans = query_value(conn, f"SELECT SUM(loans) FROM gold.agg_vintage WHERE issue_month = '{month}'")

    if silver_rows == 0:
        raise RuntimeError(f"{month}: COPY loaded 0 rows into silver.loans")
    if fact_rows != silver_rows:
        raise RuntimeError(f"{month}: fact_loan has {fact_rows:,} rows, silver.loans has {silver_rows:,}")
    if missing_ids > 0:
        raise RuntimeError(f"{month}: {missing_ids:,} fact rows have a missing dimension ID")
    if agg_loans != fact_rows:
        raise RuntimeError(f"{month}: agg_vintage counts {agg_loans:,} loans, fact_loan has {fact_rows:,}")

    print(f"[check_month] {month}: OK, {fact_rows:,} rows in silver, fact and aggregate, no missing IDs")


# ---------- MAIN ----------
def main():
    # Glue passes job parameters on the command line, e.g. --workgroup_name lending-wg
    args = getResolvedOptions(sys.argv, ["workgroup_name", "database", "secret_arn"])
    conn = {
        "WorkgroupName": args["workgroup_name"],
        "Database": args["database"],
        "SecretArn": args["secret_arn"],
    }

    last_processed = read_tracker(SILVER_TRACKER_KEY)
    last_loaded = read_tracker(GOLD_TRACKER_KEY)
    print(f"[main] Last processed (Silver): {last_processed} | last loaded (Gold): {last_loaded}")

    months = months_to_load(last_loaded, last_processed)
    if not months:
        print("[main] Nothing new from Silver. Redshift is up to date.")
        return

    print(f"[main] {len(months)} month(s) to load: {months[0]} to {months[-1]}")
    for month in months:
        load_month(conn, month)
        check_month(conn, month)
        update_tracker(GOLD_TRACKER_KEY, month)   # only after the month passed its checks


if __name__ == "__main__":
    main()