-- Durable remote Agent commands.  The deterministic operation key makes
-- replay after a process crash safe for runners that honor Idempotency-Key.
CREATE TABLE agent_command_outbox (
    command_id TEXT NOT NULL CHECK(length(command_id) > 0),
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) > 0),
    owner_id TEXT NOT NULL CHECK(length(owner_id) > 0),
    device_id TEXT NOT NULL CHECK(length(device_id) > 0),
    task_id TEXT NOT NULL CHECK(length(task_id) > 0),
    runner_task_id TEXT,
    command_type TEXT NOT NULL CHECK(command_type IN ('cancel')),
    operation_key TEXT NOT NULL CHECK(length(operation_key) > 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at REAL NOT NULL DEFAULT 0 CHECK(next_attempt_at >= 0),
    last_error TEXT,
    outcome TEXT CHECK(outcome IN ('cancelled', 'cancel_failed', 'terminal_won')),
    force_remote INTEGER NOT NULL DEFAULT 0 CHECK(force_remote IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (tenant_id, owner_id, command_id),
    UNIQUE (operation_key),
    UNIQUE (tenant_id, owner_id, task_id, command_type),
    FOREIGN KEY (tenant_id, owner_id, task_id)
        REFERENCES agent_tasks(tenant_id, owner_id, task_id) ON DELETE CASCADE
);

CREATE INDEX idx_agent_command_outbox_due
    ON agent_command_outbox(completed_at, next_attempt_at, created_at, command_id);

CREATE INDEX idx_agent_command_outbox_task
    ON agent_command_outbox(tenant_id, owner_id, task_id, completed_at);

-- A v35 process may have committed cancel_requested immediately before the
-- upgrade.  Materialize those commands in the same migration transaction so
-- startup cannot silently strand the durable user intent.
INSERT INTO agent_command_outbox (
    command_id,
    tenant_id,
    owner_id,
    device_id,
    task_id,
    runner_task_id,
    command_type,
    operation_key,
    attempts,
    next_attempt_at,
    last_error,
    outcome,
    force_remote,
    created_at,
    updated_at,
    completed_at
)
SELECT
    'agent_cmd_m36_' || lower(hex(randomblob(16))),
    task.tenant_id,
    task.owner_id,
    task.device_id,
    task.task_id,
    task.runner_task_id,
    'cancel',
    'agent-cancel-' || lower(hex(randomblob(32))),
    0,
    0,
    NULL,
    NULL,
    0,
    task.submitted_at,
    CURRENT_TIMESTAMP,
    NULL
FROM agent_tasks AS task
WHERE task.state = 'cancel_requested';
