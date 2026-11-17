-- Gold aggregate: cumulative charge-off curve by vintage, grade and months on book.
CREATE TABLE gold.agg_vintage_curve (
    issue_month              VARCHAR(7),
    grade                    VARCHAR(4),
    mob                      INT,
    loans                    BIGINT,
    funded_amount            DECIMAL(15,2),
    chargeoff_loans          BIGINT,
    gross_chargeoff_amount   DECIMAL(15,2),
    net_chargeoff_amount     DECIMAL(15,2),
    cum_gross_chargeoff_pct  DECIMAL(7,3)
)
SORTKEY (issue_month, mob);


-- Fill gold.agg_vintage_curve
INSERT INTO gold.agg_vintage_curve
WITH snap AS (
    SELECT MAX(last_payment_date) AS snapshot_date FROM gold.fact_loan
),
loans AS (                      -- one row per loan, with what the curve needs
    SELECT TO_CHAR(f.issue_date, 'YYYY-MM') AS issue_month,
           g.grade,
           f.issue_date,
           f.loan_amount,
           CASE WHEN s.loan_status_category IN ('charged_off', 'default') THEN 1 ELSE 0 END AS is_chargeoff,
           DATEDIFF(month, f.issue_date, COALESCE(f.last_payment_date, f.issue_date)) AS chargeoff_mob,
           f.loan_amount - f.principal_received                AS gross_loss,
           f.loan_amount - f.principal_received - f.recoveries AS net_loss
    FROM gold.fact_loan AS f
    JOIN gold.dim_grade       AS g ON f.grade_id  = g.grade_id
    JOIN gold.dim_loan_status AS s ON f.status_id = s.status_id
),
cohorts AS (                    -- one row per vintage and grade
    SELECT issue_month, grade,
           MIN(issue_date)  AS start_date,
           COUNT(*)         AS loans,
           SUM(loan_amount) AS funded_amount
    FROM loans
    GROUP BY 1, 2
),
mobs AS (                       -- the numbers 0, 1, 2 ... one per month in dim_date
    SELECT ROW_NUMBER() OVER (ORDER BY month_start) - 1 AS mob FROM gold.dim_date
),
grid AS (                       -- every vintage x every MOB it has reached by the snapshot
    SELECT c.issue_month, c.grade, c.loans, c.funded_amount, m.mob
    FROM cohorts AS c
    CROSS JOIN mobs AS m
    CROSS JOIN snap AS sn
    WHERE m.mob <= DATEDIFF(month, c.start_date, sn.snapshot_date)
)
SELECT gr.issue_month,
       gr.grade,
       gr.mob,
       gr.loans,
       gr.funded_amount,
       COUNT(l.issue_month)              AS chargeoff_loans,
       COALESCE(SUM(l.gross_loss), 0)    AS gross_chargeoff_amount,
       COALESCE(SUM(l.net_loss), 0)      AS net_chargeoff_amount,
       COALESCE(SUM(l.gross_loss), 0) / gr.funded_amount * 100 AS cum_gross_chargeoff_pct
FROM grid AS gr
LEFT JOIN loans AS l
       ON  l.issue_month   = gr.issue_month
       AND l.grade         = gr.grade
       AND l.is_chargeoff  = 1
       AND l.chargeoff_mob <= gr.mob        -- "by this MOB" makes it cumulative
GROUP BY gr.issue_month, gr.grade, gr.mob, gr.loans, gr.funded_amount;