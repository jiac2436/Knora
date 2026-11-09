-- Run as schema owner before deploying the updated API and Worker.
CREATE TABLE IF NOT EXISTS feishu_document_artifacts (
    revision_id VARCHAR(36) PRIMARY KEY REFERENCES source_revisions(id),
    document_id VARCHAR(200) NOT NULL,
    document_revision_id INTEGER NOT NULL,
    cli_version VARCHAR(80) NOT NULL
);

-- Same owner-scoped RLS direction as rls.sql. Application roles must set pka.owner_id.
ALTER TABLE feishu_document_artifacts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS feishu_document_owner ON feishu_document_artifacts;
CREATE POLICY feishu_document_owner ON feishu_document_artifacts
  USING (EXISTS (
    SELECT 1 FROM source_revisions r JOIN sources s ON s.id = r.source_id
    WHERE r.id = feishu_document_artifacts.revision_id
      AND s.owner_id = current_setting('pka.owner_id', true)
  ));
