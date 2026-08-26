-- Add immutable migration identity fields.  The migrator fills every
-- currently locatable applied version, including v030 itself, inside the same
-- BEGIN IMMEDIATE transaction that applies this schema change.  Columns stay
-- nullable only for historical versions whose SQL file is no longer shipped.

ALTER TABLE schema_version ADD COLUMN migration_name TEXT;
ALTER TABLE schema_version ADD COLUMN content_sha256 TEXT;
