CREATE TABLE audit_events (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, actor_id) REFERENCES users (tenant_id, id)
) STRICT;

CREATE UNIQUE INDEX audit_creation_per_subject ON audit_events (tenant_id, action, subject_id)
    WHERE action IN ('ticket.create', 'approval.request');

CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'Audit events are append-only');
END;

CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'Audit events are append-only');
END;

CREATE TRIGGER audit_events_no_replace BEFORE INSERT ON audit_events
WHEN EXISTS (
    SELECT 1 FROM audit_events WHERE tenant_id = NEW.tenant_id AND id = NEW.id
) OR (
    NEW.action IN ('ticket.create', 'approval.request')
    AND EXISTS (
        SELECT 1 FROM audit_events
        WHERE tenant_id = NEW.tenant_id AND action = NEW.action AND subject_id = NEW.subject_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'Audit events are append-only');
END;

CREATE TABLE approval_requests (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    basis_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL CHECK (expires_at > created_at),
    decided_by TEXT,
    decided_at TEXT,
    consumed_ticket_id TEXT,
    creation_audit_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
    FOREIGN KEY (tenant_id, requested_by) REFERENCES users (tenant_id, id),
    FOREIGN KEY (tenant_id, decided_by) REFERENCES users (tenant_id, id),
    FOREIGN KEY (tenant_id, consumed_ticket_id) REFERENCES escalation_tickets (tenant_id, id),
    FOREIGN KEY (tenant_id, creation_audit_id) REFERENCES audit_events (tenant_id, id),
    CHECK ((status = 'pending') = (decided_by IS NULL AND decided_at IS NULL)),
    CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CHECK (consumed_ticket_id IS NULL OR status = 'approved')
) STRICT;

CREATE UNIQUE INDEX approval_pending_basis ON approval_requests
    (tenant_id, sandbox_id, run_id, invoice_id, requested_by, scope_digest, basis_digest)
    WHERE status = 'pending';

CREATE TABLE command_receipts (
    tenant_id TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('ticket.create', 'approval.request')),
    command_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    result_id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, sandbox_id, run_id, actor_id, action, command_key),
    FOREIGN KEY (tenant_id, actor_id) REFERENCES users (tenant_id, id),
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
    FOREIGN KEY (tenant_id, audit_id) REFERENCES audit_events (tenant_id, id)
) STRICT;

PRAGMA user_version = 2;
