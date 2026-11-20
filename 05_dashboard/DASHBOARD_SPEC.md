# Credit Risk Monitor: Power BI build spec

This file is the single source of truth for building the Power BI report in
`05_dashboard/credit_risk_monitoring.pbip`. Claude Code reads it when building the
model (with the `powerbi-modeling` MCP server) and the pages (by writing PBIR files).

---

## 1. Context

- Portfolio: 1.79M US unsecured personal loans issued 2015-01 to 2018-12 (48 vintages).
- Source: Amazon Redshift Serverless, database `lending_db`, schema `gold`, loaded
  monthly by the AWS pipeline in this repo (`03_warehouse`).
- Power BI connects with the read-only user `bi_reader`, in **Import** mode.
- Users: Credit Risk team (vintage, monitoring), Collections (delinquency),
  leadership (overview).
- The data is a snapshot: each loan has its status on the snapshot date, not a
  monthly history. Charge-off timing is approximated from `last_payment_date`.

## 2. Tables (all in `gold`)

| Table | Grain | Key columns |
|---|---|---|
| `fact_loan` | one row per loan | `loan_key`, `grade_id`, `purpose_id`, `geography_id`, `status_id`, `issue_date`, `last_payment_date`, `term_months`, `loan_amount`, `installment`, `interest_rate` (percent number, e.g. 13.56), `annual_income`, `dti`, `total_payment`, `principal_received`, `interest_received`, `recoveries`, `outstanding_principal`, `is_bad` |
| `dim_grade` | one row per sub-grade | `grade_id`, `grade`, `sub_grade` |
| `dim_purpose` | one row per purpose | `purpose_id`, `purpose` |
| `dim_geography` | one row per state | `geography_id`, `state` |
| `dim_loan_status` | one row per status | `status_id`, `loan_status`, `loan_status_category`, `is_bad`, `dpd_bucket`, `bucket_order`, `is_active` |
| `dim_date` | one row per month | `month_start`, `year`, `quarter`, `month_num`, `month_name`, `year_month` |
| `agg_vintage` | issue month x grade | `issue_month`, `grade`, `loans`, `funded_amount`, `bad_loans`, `bad_rate_pct`, `avg_interest_rate` |
| `agg_vintage_curve` | issue month x grade x MOB | `issue_month`, `grade`, `mob`, `loans`, `funded_amount`, `chargeoff_loans`, `gross_chargeoff_amount`, `net_chargeoff_amount`, `cum_gross_chargeoff_pct` |
| `agg_grade_monitoring` | issue month | `issue_month`, `loans`, `bad_loans`, `bad_rate_pct`, `ks`, `gini`, `psi`, `psi_status` |

Charged-off loan = `dim_loan_status[loan_status_category]` in `charged_off`, `default`.
30+ DPD = `bucket_order` 4 (31-120 DPD) or 5 (120+ DPD), among active loans (`is_active = 1`).

## 3. Model

**Table names in Power BI:** drop the `gold` prefix (`fact_loan`, `dim_grade`, ...).

**Calculated table** `dim_grade_letter` = `DISTINCT(dim_grade[grade])`, column renamed `Grade`.

**Relationships** (single direction, many-to-one):

| From (many) | To (one) | Active |
|---|---|---|
| `fact_loan[grade_id]` | `dim_grade[grade_id]` | yes |
| `fact_loan[purpose_id]` | `dim_purpose[purpose_id]` | yes |
| `fact_loan[geography_id]` | `dim_geography[geography_id]` | yes |
| `fact_loan[status_id]` | `dim_loan_status[status_id]` | yes |
| `fact_loan[issue_date]` | `dim_date[month_start]` | yes |
| `fact_loan[last_payment_date]` | `dim_date[month_start]` | **no** (inactive) |
| `dim_grade[grade]` | `dim_grade_letter[Grade]` | yes |
| `agg_vintage[issue_month]` | `dim_date[year_month]` | yes |
| `agg_vintage[grade]` | `dim_grade_letter[Grade]` | yes |
| `agg_vintage_curve[issue_month]` | `dim_date[year_month]` | yes |
| `agg_vintage_curve[grade]` | `dim_grade_letter[Grade]` | yes |
| `agg_grade_monitoring[issue_month]` | `dim_date[year_month]` | yes |

**Mark** `dim_date` as a date table on `month_start`.

**Sort by column:** `dim_date[month_name]` by `month_num`, `dim_loan_status[dpd_bucket]` by `bucket_order`.

**Hide** every `_id` column, `loan_key`, `fact_loan[is_bad]`, `dim_loan_status[is_bad]`,
`bucket_order`, `is_active`, and all raw numeric columns that a measure replaces.

**Measures table:** create an empty table `_Measures` and put every measure there,
in the display folders below.

## 4. Measures

| Folder | Measure | DAX | Format |
|---|---|---|---|
| Portfolio | Loans | `COUNTROWS(fact_loan)` | `#,0` |
| Portfolio | Funded Amount | `SUM(fact_loan[loan_amount])` | `$#,0.0,,"M"` |
| Portfolio | Avg Loan Size | `DIVIDE([Funded Amount], [Loans])` | `$#,0` |
| Portfolio | Avg Interest Rate | `DIVIDE(AVERAGE(fact_loan[interest_rate]), 100)` | `0.00%` |
| Portfolio | Loan Share | `DIVIDE([Loans], CALCULATE([Loans], ALLSELECTED(dim_grade)))` | `0.0%` |
| Risk | Bad Loans | `SUM(fact_loan[is_bad])` | `#,0` |
| Risk | Bad Rate | `DIVIDE([Bad Loans], [Loans])` | `0.0%` |
| Losses | Charged-off Loans | `CALCULATE([Loans], dim_loan_status[loan_status_category] IN {"charged_off", "default"})` | `#,0` |
| Losses | Gross Charge-off | `CALCULATE(SUMX(fact_loan, fact_loan[loan_amount] - fact_loan[principal_received]), dim_loan_status[loan_status_category] IN {"charged_off", "default"})` | `$#,0.0,,"M"` |
| Losses | Recoveries | `CALCULATE(SUM(fact_loan[recoveries]), dim_loan_status[loan_status_category] IN {"charged_off", "default"})` | `$#,0.0,,"M"` |
| Losses | Net Charge-off | `[Gross Charge-off] - [Recoveries]` | `$#,0.0,,"M"` |
| Losses | Net Charge-off Rate | `DIVIDE([Net Charge-off], [Funded Amount])` | `0.00%` |
| Losses | Recovery Rate | `DIVIDE([Recoveries], [Gross Charge-off])` | `0.0%` |
| Losses | Avg MOB at Charge-off | `CALCULATE(AVERAGEX(fact_loan, DATEDIFF(fact_loan[issue_date], COALESCE(fact_loan[last_payment_date], fact_loan[issue_date]), MONTH)), dim_loan_status[loan_status_category] IN {"charged_off", "default"})` | `0.0` |
| Delinquency | Active Loans | `CALCULATE([Loans], dim_loan_status[is_active] = 1)` | `#,0` |
| Delinquency | 30+ DPD Loans | `CALCULATE([Loans], dim_loan_status[bucket_order] IN {4, 5})` | `#,0` |
| Delinquency | 30+ DPD Rate | `DIVIDE([30+ DPD Loans], [Active Loans])` | `0.00%` |
| Delinquency | 120+ DPD Loans | `CALCULATE([Loans], dim_loan_status[bucket_order] = 5)` | `#,0` |
| Profitability | Interest Earned | `SUM(fact_loan[interest_received])` | `$#,0.0,,"M"` |
| Profitability | Net Yield | `DIVIDE([Interest Earned] - [Net Charge-off], [Funded Amount])` | `0.0%` |
| Vintage | Cum Gross CO % | `DIVIDE(SUM(agg_vintage_curve[gross_chargeoff_amount]), SUM(agg_vintage_curve[funded_amount]))` | `0.00%` |
| Vintage | CO % at MOB 12 | `CALCULATE([Cum Gross CO %], agg_vintage_curve[mob] = 12)` | `0.00%` |
| Vintage | CO % at MOB 24 | `CALCULATE([Cum Gross CO %], agg_vintage_curve[mob] = 24)` | `0.00%` |
| Vintage | CO % at MOB 36 | `CALCULATE([Cum Gross CO %], agg_vintage_curve[mob] = 36)` | `0.00%` |
| Vintage | Vintages | `DISTINCTCOUNT(agg_vintage_curve[issue_month])` | `#,0` |
| Monitoring | Avg KS | `AVERAGE(agg_grade_monitoring[ks])` | `0.0` |
| Monitoring | Avg Gini | `AVERAGE(agg_grade_monitoring[gini])` | `0.000` |
| Monitoring | Latest PSI | `VAR m = MAX(agg_grade_monitoring[issue_month]) RETURN CALCULATE(MAX(agg_grade_monitoring[psi]), agg_grade_monitoring[issue_month] = m)` | `0.000` |
| Monitoring | Vintages to Monitor | `CALCULATE(COUNTROWS(agg_grade_monitoring), agg_grade_monitoring[psi_status] = "Monitor")` | `#,0` |
| Monitoring | Vintages with Shift | `CALCULATE(COUNTROWS(agg_grade_monitoring), agg_grade_monitoring[psi_status] = "Shift")` | `#,0` |

Before setting formats, run a DAX query to check the scale of `interest_rate`
(expected 5 to 31, a percent number). If it is already a fraction, drop the `/ 100`.

## 5. Style (from the Business Insights 360 report)

- Page 1920 x 1080, background `#F7F7F2`. Theme file: `05_dashboard/credit_risk_theme.json`.
- Fonts: Segoe UI family. Titles Segoe UI Semibold, orange `#C86843`.
- Containers: background `#FCFCFA`, 1 px border `#BFBFBF`, radius 10, soft drop shadow.
- Grade colours A to G: `#38624D #5F7F3B #899878 #CDCF89 #F0AC60 #D87B43 #DA3D4D`.
- Accent: amber `#D87B43`. Dark ink `#222725`, near-black `#121113`, pale olive `#E4E6C3`, olive `#899878`.
- Text colours: values `#444444`, labels `#5E5E5E`.
- Red only for risk. No gradients, no emoji.

## 6. Shared page chrome (on every page, same positions)

| Element | x | y | w | h | Details |
|---|---|---|---|---|---|
| Left rail background | 0 | 0 | 160 | 1080 | rectangle `#F7F7F2`, right border `#E2E2DA` |
| Logo placeholder | 30 | 24 | 100 | 76 | text "[LOGO]", dashed olive border |
| Brand block | 15 | 116 | 130 | 140 | shape, top corners rounded 25, fill `#121113`, text "Credit Risk Monitor", Segoe UI 18 bold italic, `#F7F7F2` |
| Nav button: Overview | 15 | 263 | 130 | 140 | page navigation to Overview |
| Nav button: Vintage | 15 | 411 | 130 | 140 | page navigation to Vintage |
| Nav button: Delinquency | 15 | 559 | 130 | 140 | page navigation to Delinquency |
| Nav button: Monitoring | 15 | 707 | 130 | 140 | page navigation to Monitoring |
| Nav button: Info | 15 | 1000 | 130 | 66 | no action yet |
| Header band | 170 | 14 | 1673 | 205 | rectangle fill `#899878`, radius 12, 5 px outline `#F7F7F2`, glow `#787878` 50% |
| "FILTERS" label | 190 | 60 | 40 | 115 | text rotated 270, Segoe UI Semibold 18, `#121113` |
| Slicer: Issue year | 245 | 40 | 330 | 70 | `dim_date[year]`, tile buttons, multi-select |
| Slicer: Term | 245 | 120 | 330 | 70 | `fact_loan[term_months]`, tile buttons |
| Slicer: Grade | 600 | 80 | 200 | 70 | `dim_grade_letter[Grade]`, dropdown |
| Slicer: Purpose | 816 | 80 | 200 | 70 | `dim_purpose[purpose]`, dropdown |
| Slicer: State | 1032 | 80 | 200 | 70 | `dim_geography[state]`, dropdown |
| Abbreviations text | 1260 | 40 | 560 | 140 | "MOB = Months on Book \| DPD = Days Past Due \| NCO = Net Charge-off \| KS = Kolmogorov-Smirnov \| PSI = Population Stability Index", plus "Amounts in $ millions" |
| Support button | 1852 | 47 | 56 | 137 | text "Support", vertical |
| Footer | 1866 | 760 | 36 | 297 | text "Designed by: Ajaychandra Arekal", rotated 270 |

Nav button style: fill `#E4E6C3`, 5 px outline `#222725`, top corners rounded 25,
Segoe UI 18 bold `#121113`. On the current page, that button is filled `#222725`
with text `#F7F7F2`. Content area: x 170 to 1843, y 236 to 1066.

Slicers are synced across all four pages (same group).

## 7. Page 1: Overview

| Visual | x | y | w | h | Fields |
|---|---|---|---|---|---|
| KPI strip (6 cards or one multi-card) | 170 | 236 | 1673 | 126 | Funded Amount, Loans, Avg Interest Rate, Bad Rate, Net Charge-off Rate, 30+ DPD Rate. Left accent bar `#D87B43`, value Segoe UI Bold, label below |
| Line and clustered column: "Monthly originations" | 170 | 376 | 1000 | 340 | x `dim_date[month_start]` (continuous by month), columns Funded Amount (`#899878`), line Avg Interest Rate (`#D87B43`, secondary axis). Subtitle "Funded amount (bars) and average interest rate (line) by issue month" |
| 100% stacked column: "Grade mix by issue year" | 1184 | 376 | 659 | 340 | x `dim_date[year]`, legend `dim_grade[grade]`, values Loans. Grade colours A to G. Subtitle "Is the book shifting toward riskier grades?" |
| Clustered column: "Bad rate by grade" | 170 | 730 | 820 | 336 | x `dim_grade[grade]`, y Bad Rate, data labels on, one colour per grade. Subtitle "Share of loans that went late, defaulted or were charged off" |
| Matrix: "Risk-adjusted yield by grade" | 1004 | 730 | 839 | 336 | rows `dim_grade[grade]`, values Funded Amount, Interest Earned, Net Charge-off, Net Yield. Subtitle "Does the higher rate on riskier grades cover their losses?" |

## 8. Page 2: Vintage

| Visual | x | y | w | h | Fields |
|---|---|---|---|---|---|
| KPI strip (5) | 170 | 236 | 1673 | 126 | Vintages, CO % at MOB 12, CO % at MOB 24, CO % at MOB 36, plus a text card "Grade G vs A at MOB 36" |
| Line chart: "Cumulative charge-off curves" | 170 | 376 | 1000 | 420 | x `agg_vintage_curve[mob]`, y Cum Gross CO %, legend `dim_date[year]`. Subtitle "Gross charge-off as % of funded amount, by months on book (static pool)" |
| Matrix: "Charge-off % by grade at fixed ages" | 1184 | 376 | 659 | 420 | rows `dim_grade_letter[Grade]`, values CO % at MOB 12, 24, 36, background colour scale `#F7F7F2` to `#F0AC60` |
| Matrix heatmap: "Bad rate heatmap: issue quarter x grade" | 170 | 810 | 1000 | 256 | rows `dim_date[year]` and `quarter`, columns `dim_grade_letter[Grade]`, value Bad Rate, colour scale `#F7F7F2` to `#DA3D4D` |
| Text box: "Reading this page" | 1184 | 810 | 659 | 256 | "Recent vintages look safer only because they are younger. Compare vintages at the same months on book, never by calendar date. Charge-off month is taken from the last payment date, since the data has no monthly history." |

## 9. Page 3: Delinquency

| Visual | x | y | w | h | Fields |
|---|---|---|---|---|---|
| KPI strip (6) | 170 | 236 | 1673 | 126 | Active Loans, 30+ DPD Rate, 120+ DPD Loans, Gross Charge-off, Recovery Rate, Avg MOB at Charge-off |
| Bar: "Active loans by DPD bucket" | 170 | 376 | 540 | 360 | y `dim_loan_status[dpd_bucket]` (filter `is_active = 1`), x Loans |
| Column: "30+ DPD rate by grade" | 724 | 376 | 540 | 360 | x `dim_grade[grade]`, y 30+ DPD Rate |
| Line: "30+ DPD rate by issue month" | 1278 | 376 | 565 | 360 | x `dim_date[month_start]`, y 30+ DPD Rate |
| Bar: "30+ DPD rate by state (top 10)" | 170 | 750 | 700 | 316 | y `dim_geography[state]`, x 30+ DPD Rate, Top N 10 by 30+ DPD Rate |
| Matrix: "Collections by grade" | 884 | 750 | 959 | 316 | rows `dim_grade[grade]`, values Charged-off Loans, Gross Charge-off, Recoveries, Recovery Rate, Avg MOB at Charge-off. Footnote "Snapshot data: roll rates between buckets need monthly snapshots." |

## 10. Page 4: Monitoring

| Visual | x | y | w | h | Fields |
|---|---|---|---|---|---|
| KPI strip (5) | 170 | 236 | 1673 | 126 | Avg KS, Avg Gini, Latest PSI, Vintages to Monitor, Vintages with Shift |
| Line: "Discrimination over time: KS and Gini" | 170 | 376 | 1000 | 400 | x `dim_date[month_start]`, KS (`#38624D`, left axis), Gini (`#D87B43`, right axis) |
| Column: "Population stability (PSI)" | 1184 | 376 | 659 | 400 | x `dim_date[month_start]`, y `agg_grade_monitoring[psi]`, constant lines 0.10 and 0.25 (dashed `#222725`), colour by `psi_status` (Stable `#899878`, Monitor `#F0AC60`, Shift `#DA3D4D`) |
| Column: "Rank ordering: bad rate by sub-grade" | 170 | 790 | 1000 | 276 | x `dim_grade[sub_grade]` (A1 to G5), y Bad Rate |
| Clustered column: "Grade mix: 2015 baseline vs latest year" | 1184 | 790 | 659 | 276 | x `dim_grade[grade]`, y Loan Share, legend `dim_date[year]` filtered to 2015 and 2018 |

Purpose and State slicers do not filter the `agg_` tables. On Vintage and
Monitoring, set those two slicers to "no interaction" with the agg visuals.
