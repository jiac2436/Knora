-- Run as schema owner before deploying the image-aware API and Worker.
CREATE TABLE IF NOT EXISTS source_images (
    id VARCHAR(36) PRIMARY KEY,
    source_id VARCHAR(36) NOT NULL REFERENCES sources(id),
    revision_id VARCHAR(36) NOT NULL REFERENCES source_revisions(id),
    ordinal INTEGER NOT NULL,
    block_id VARCHAR(200) NOT NULL,
    caption VARCHAR(500) NOT NULL,
    status VARCHAR(20) NOT NULL,
    failure_reason VARCHAR(500) NOT NULL,
    ocr_text TEXT NOT NULL,
    file_key VARCHAR(36),
    sha256 VARCHAR(64),
    size_bytes INTEGER,
    mime_type VARCHAR(40),
    width INTEGER,
    height INTEGER,
    CONSTRAINT uq_source_image_ordinal UNIQUE (revision_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_source_images_source_id ON source_images(source_id);
CREATE INDEX IF NOT EXISTS ix_source_images_revision_id ON source_images(revision_id);
ALTER TABLE source_images ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS source_images_owner ON source_images;
CREATE POLICY source_images_owner ON source_images
  USING (EXISTS (
    SELECT 1 FROM sources s WHERE s.id = source_images.source_id
      AND s.owner_id = current_setting('pka.owner_id', true)
  ));
