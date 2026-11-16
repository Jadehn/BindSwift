-- Production: give the app the least access it needs.
-- 1) Create tables as an owner role (e.g. bindswift_owner) with schema.sql.
-- 2) Run this as that owner. The app connects as bindswift_app.
--    It can't drop tables, and it can't edit or delete the audit log.

-- CREATE ROLE bindswift_app LOGIN PASSWORD '<long random password>';

GRANT CONNECT ON DATABASE bindswift TO bindswift_app;
GRANT USAGE ON SCHEMA public TO bindswift_app;

GRANT SELECT, INSERT, UPDATE         ON submissions, submission_documents, admins TO bindswift_app;
GRANT SELECT, INSERT, UPDATE         ON carrier_quotes TO bindswift_app;
GRANT SELECT, INSERT                 ON api_hits TO bindswift_app;
GRANT SELECT, INSERT, UPDATE         ON policies, certificates TO bindswift_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON client_sessions TO bindswift_app;
GRANT SELECT, INSERT, UPDATE         ON email_verifications TO bindswift_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON admin_sessions TO bindswift_app;
GRANT SELECT, INSERT                 ON login_attempts, audit_log TO bindswift_app;   -- append-only
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bindswift_app;

REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM bindswift_app;

GRANT SELECT, INSERT, UPDATE         ON app_settings TO bindswift_app;
