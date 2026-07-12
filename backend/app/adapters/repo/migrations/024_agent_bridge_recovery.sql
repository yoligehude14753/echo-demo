-- Durable AgentOS stream ownership and post-commit projection recovery.
ALTER TABLE agent_task_events ADD COLUMN raw_kind TEXT;
ALTER TABLE agent_task_events ADD COLUMN projected_at TEXT;
ALTER TABLE agent_tasks ADD COLUMN bridge_completed_at TEXT;

-- Events committed by pre-024 builds already ran the legacy synchronous
-- projection path.  Only events written after this migration start pending.
UPDATE agent_task_events
SET projected_at = created_at
WHERE projected_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_task_events_pending_projection
    ON agent_task_events(tenant_id, owner_id, task_id, seq)
    WHERE projected_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_tasks_bridge_recovery
    ON agent_tasks(tenant_id, owner_id, bridge_completed_at, state, submitted_at);
