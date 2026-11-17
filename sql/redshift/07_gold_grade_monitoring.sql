-- Gold aggregate: monitoring of the Lending Club grade per vintage (KS, Gini, PSI).
CREATE TABLE gold.agg_grade_monitoring (
    issue_month   VARCHAR(7),
    loans         BIGINT,
    bad_loans     BIGINT,
    bad_rate_pct  DECIMAL(5,2),
    ks            DECIMAL(5,2),
    gini          DECIMAL(5,3),
    psi           DECIMAL(6,4),
    psi_status    VARCHAR(16)
)
SORTKEY (issue_month);

-- Fill gold.agg_grade_monitoring
INSERT INTO gold.agg_grade_monitoring
WITH base AS (                  -- loans, bads and goods per vintage and sub-grade
    SELECT TO_CHAR(f.issue_date, 'YYYY-MM') AS issue_month,
           g.grade,
           g.sub_grade,
           COUNT(*)                     AS loans,
           SUM(f.is_bad)                AS bads,
           COUNT(*) - SUM(f.is_bad)     AS goods
    FROM gold.fact_loan AS f
    JOIN gold.dim_grade AS g ON f.grade_id = g.grade_id
    GROUP BY 1, 2, 3
),
totals AS (                     -- per vintage
    SELECT issue_month, SUM(loans) AS loans, SUM(bads) AS bads, SUM(goods) AS goods
    FROM base
    GROUP BY 1
),
cumulative AS (                 -- running % of bads and goods, worst sub-grade first
    SELECT b.issue_month,
           b.sub_grade,
           SUM(b.bads)  OVER (PARTITION BY b.issue_month ORDER BY b.sub_grade DESC ROWS UNBOUNDED PRECEDING)::FLOAT
               / NULLIF(t.bads, 0)  AS cum_bad,
           SUM(b.goods) OVER (PARTITION BY b.issue_month ORDER BY b.sub_grade DESC ROWS UNBOUNDED PRECEDING)::FLOAT
               / NULLIF(t.goods, 0) AS cum_good
    FROM base AS b
    JOIN totals AS t ON b.issue_month = t.issue_month
),
steps AS (                      -- each point with the point before it
    SELECT issue_month, cum_bad, cum_good,
           COALESCE(LAG(cum_bad)  OVER (PARTITION BY issue_month ORDER BY sub_grade DESC), 0) AS prev_bad,
           COALESCE(LAG(cum_good) OVER (PARTITION BY issue_month ORDER BY sub_grade DESC), 0) AS prev_good
    FROM cumulative
),
ks_gini AS (
    SELECT issue_month,
           MAX(ABS(cum_bad - cum_good)) * 100                                  AS ks,
           2 * SUM((cum_good - prev_good) * (cum_bad + prev_bad) / 2) - 1      AS gini
    FROM steps
    GROUP BY 1
),
grade_mix AS (                  -- share of loans in each grade, per vintage
    SELECT b.issue_month, b.grade, SUM(b.loans)::FLOAT / t.loans AS share
    FROM base AS b
    JOIN totals AS t ON b.issue_month = t.issue_month
    GROUP BY b.issue_month, b.grade, t.loans
),
baseline AS (                   -- the same shares for all of 2015
    SELECT grade, SUM(loans)::FLOAT / (SELECT SUM(loans) FROM base WHERE issue_month LIKE '2015-%') AS share
    FROM base
    WHERE issue_month LIKE '2015-%'
    GROUP BY 1
),
psi AS (
    SELECT m.issue_month,
           SUM( (GREATEST(m.share, 0.0001) - GREATEST(b.share, 0.0001))
                * LN(GREATEST(m.share, 0.0001) / GREATEST(b.share, 0.0001)) ) AS psi
    FROM grade_mix AS m
    JOIN baseline AS b ON m.grade = b.grade
    GROUP BY 1
)
SELECT t.issue_month,
       t.loans,
       t.bads,
       CAST(t.bads::FLOAT / t.loans * 100 AS DECIMAL(5,2)),
       CAST(k.ks AS DECIMAL(5,2)),
       CAST(k.gini AS DECIMAL(5,3)),
       CAST(p.psi AS DECIMAL(6,4)),
       CASE WHEN p.psi < 0.10 THEN 'Stable'
            WHEN p.psi < 0.25 THEN 'Monitor'
            ELSE 'Shift' END
FROM totals AS t
JOIN ks_gini AS k ON t.issue_month = k.issue_month
JOIN psi     AS p ON t.issue_month = p.issue_month;
