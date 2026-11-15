-- SQL practice on your own Bindswift data.
-- Run in psql:  psql "$DATABASE_URL" -f queries.sql   (or paste one at a time)

-- 1. Review queue: oldest pending first (SELECT, WHERE, ORDER BY)
SELECT id, legal_name, trade, county, created_at
FROM submissions
WHERE status = 'pending_review'
ORDER BY created_at;

-- 2. Pipeline by status (GROUP BY, aggregate)
SELECT status, count(*) AS submissions, sum(annual_payroll) AS total_payroll
FROM submissions
GROUP BY status
ORDER BY submissions DESC;

-- 3. Which trades and boroughs are applying? (GROUP BY two columns)
SELECT trade, county, count(*) AS n, round(avg(annual_revenue)) AS avg_revenue
FROM submissions
GROUP BY trade, county
ORDER BY n DESC;

-- 4. Read inside JSONB: contractors who need a GC added as additional insured
SELECT id, legal_name, answers->>'holderName' AS certificate_holder
FROM submissions
WHERE answers->>'gcReq' = 'Y';

-- 5. JSONB arrays: who works in Manhattan?
SELECT id, legal_name
FROM submissions
WHERE answers->'areas' ? 'Manhattan';

-- 6. Turnaround time: hours from submission to your first review (JOIN-free date math)
SELECT id, legal_name,
       round(extract(epoch FROM reviewed_at - created_at) / 3600, 1) AS hours_to_review
FROM submissions
WHERE reviewed_at IS NOT NULL
ORDER BY hours_to_review DESC;

-- 7. JOIN: who approved what, and when (submissions x audit_log x admins)
SELECT s.id, s.legal_name, l.detail->>'to' AS new_status, a.email AS by_admin, l.created_at
FROM audit_log l
JOIN submissions s ON s.id = l.submission_id
JOIN admins a      ON a.id = l.admin_id
WHERE l.action = 'status_change'
ORDER BY l.created_at DESC;

-- 8. Window function: running count of submissions per week
SELECT date_trunc('week', created_at) AS week,
       count(*) AS this_week,
       sum(count(*)) OVER (ORDER BY date_trunc('week', created_at)) AS running_total
FROM submissions
GROUP BY week
ORDER BY week;

-- 9. CTE: renewals coming up in the next 60 days (bound policies)
WITH bound AS (
  SELECT id, legal_name, contact_email, effective_date + interval '1 year' AS renews_on
  FROM submissions
  WHERE status = 'bound'
)
SELECT * FROM bound
WHERE renews_on BETWEEN now() AND now() + interval '60 days'
ORDER BY renews_on;

-- 10. Security check: failed admin logins in the last 24 hours
SELECT email, count(*) AS failures, max(created_at) AS last_try
FROM login_attempts
WHERE NOT succeeded AND created_at > now() - interval '24 hours'
GROUP BY email
ORDER BY failures DESC;

-- 11. Emails that failed to send (so nothing slips through)
SELECT id, legal_name, email_error, created_at
FROM submissions
WHERE email_sent_at IS NULL AND email_error IS NOT NULL;
