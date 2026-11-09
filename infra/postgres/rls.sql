-- Production hardening direction. Run after schema creation as a database owner,
-- and use a separate non-owner application role. SQLite development does not
-- provide RLS, so the same predicates are also enforced in the service layer.
ALTER TABLE domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE experience_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE ocr_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE ocr_jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY domains_owner ON domains
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY projects_owner ON projects
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY sources_owner ON sources
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY experiences_owner ON experience_records
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY connections_owner ON agent_connections
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY audit_owner ON audit_events
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY ocr_preferences_owner ON ocr_preferences
  USING (owner_id = current_setting('pka.owner_id', true));
CREATE POLICY ocr_jobs_owner ON ocr_jobs
  USING (owner_id = current_setting('pka.owner_id', true));
