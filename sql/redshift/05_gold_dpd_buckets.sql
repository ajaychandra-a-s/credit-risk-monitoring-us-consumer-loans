-- Add DPD buckets to dim_loan_status for the delinquency report

-- 1. Add the new columns
ALTER TABLE gold.dim_loan_status ADD COLUMN dpd_bucket VARCHAR(16);
ALTER TABLE gold.dim_loan_status ADD COLUMN bucket_order INT;
ALTER TABLE gold.dim_loan_status ADD COLUMN is_active INT;

-- 2. Fill them from loan_status
UPDATE gold.dim_loan_status
SET dpd_bucket = CASE LOWER(loan_status)
                     WHEN 'current'            THEN 'Current'
                     WHEN 'in grace period'    THEN '1-15 DPD'
                     WHEN 'late (16-30 days)'  THEN '16-30 DPD'
                     WHEN 'late (31-120 days)' THEN '31-120 DPD'
                     WHEN 'default'            THEN '120+ DPD'
                     WHEN 'charged off'        THEN 'Charged off'
                     WHEN 'fully paid'         THEN 'Closed'
                 END,
    bucket_order = CASE LOWER(loan_status)
                     WHEN 'current'            THEN 1
                     WHEN 'in grace period'    THEN 2
                     WHEN 'late (16-30 days)'  THEN 3
                     WHEN 'late (31-120 days)' THEN 4
                     WHEN 'default'            THEN 5
                     WHEN 'charged off'        THEN 6
                     WHEN 'fully paid'         THEN 7
                 END,
    is_active = CASE WHEN LOWER(loan_status) IN ('charged off', 'fully paid') THEN 0 ELSE 1 END;

-- 3. Check: 7 rows, no blanks
SELECT * FROM gold.dim_loan_status ORDER BY bucket_order;