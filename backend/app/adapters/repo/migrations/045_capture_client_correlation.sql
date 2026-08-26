-- Capture observability: retain the client correlation id without changing
-- authentication, STT admission, or transcript processing semantics.
ALTER TABLE ambient_segments ADD COLUMN client_segment_id TEXT;

CREATE INDEX idx_ambient_segments_client_segment
    ON ambient_segments(client_segment_id);
