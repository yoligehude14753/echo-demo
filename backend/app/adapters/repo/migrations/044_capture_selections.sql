CREATE TABLE capture_selections (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('single', 'multi')),
    selected_device_ids_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    PRIMARY KEY (tenant_id, owner_id)
);
