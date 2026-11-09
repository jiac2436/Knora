-- Run before deploying the preview-aware API and Worker.
-- Existing image records retain the original download method.
ALTER TABLE source_images
  ADD COLUMN IF NOT EXISTS retrieval_method VARCHAR(20) NOT NULL DEFAULT 'download';
