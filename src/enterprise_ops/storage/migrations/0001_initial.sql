CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    default_policy_id TEXT,
    default_policy_version INTEGER,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    CHECK ((default_policy_id IS NULL) = (default_policy_version IS NULL)),
    FOREIGN KEY (id, default_policy_id, default_policy_version)
        REFERENCES escalation_policies (tenant_id, id, version)
) STRICT;

CREATE TABLE users (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'analyst', 'supervisor', 'simulator')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id)
) STRICT;

CREATE TABLE customers (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    id TEXT NOT NULL,
    legal_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'on_hold', 'closed')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id)
) STRICT;

CREATE TABLE customer_aliases (
    tenant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    normalized_alias TEXT NOT NULL CHECK (length(normalized_alias) > 0),
    PRIMARY KEY (tenant_id, customer_id, normalized_alias),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id)
) STRICT;

CREATE TABLE document_versions (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    PRIMARY KEY (tenant_id, id, version)
) STRICT;

CREATE TABLE document_spans (
    tenant_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version INTEGER NOT NULL,
    id TEXT NOT NULL,
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    PRIMARY KEY (tenant_id, document_id, document_version, id),
    FOREIGN KEY (tenant_id, document_id, document_version)
        REFERENCES document_versions (tenant_id, id, version)
) STRICT;

CREATE TABLE contracts (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'signed', 'terminated')),
    signed_at TEXT,
    issue_from TEXT NOT NULL,
    issue_to TEXT,
    source_document_id TEXT,
    source_document_version INTEGER,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, customer_id, id),
    UNIQUE (tenant_id, id, source_document_id, source_document_version),
    UNIQUE (tenant_id, source_document_id, source_document_version),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
    FOREIGN KEY (tenant_id, source_document_id, source_document_version)
        REFERENCES document_versions (tenant_id, id, version),
    CHECK ((source_document_id IS NULL) = (source_document_version IS NULL)),
    CHECK (status = 'draft' OR signed_at IS NOT NULL),
    CHECK (status <> 'terminated' OR issue_to IS NOT NULL),
    CHECK (issue_to IS NULL OR issue_to > issue_from)
) STRICT;

CREATE TABLE payment_clauses (
    tenant_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    id TEXT NOT NULL,
    net_days INTEGER CHECK (net_days >= 0),
    grace_days INTEGER CHECK (grace_days >= 0),
    next_weekday INTEGER CHECK (next_weekday IN (0, 1)),
    rule TEXT NOT NULL DEFAULT 'calendar',
    document_id TEXT,
    document_version INTEGER,
    span_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, contract_id, id),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_id, document_id, document_version)
        REFERENCES contracts (tenant_id, id, source_document_id, source_document_version),
    FOREIGN KEY (tenant_id, document_id, document_version, span_id)
        REFERENCES document_spans (tenant_id, document_id, document_version, id),
    CHECK ((document_id IS NULL) = (document_version IS NULL)
        AND (document_id IS NULL) = (span_id IS NULL))
) STRICT;

CREATE TABLE amendments (
    tenant_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'signed')),
    signed_at TEXT,
    activation_on TEXT,
    issue_from TEXT,
    issue_to TEXT,
    priority INTEGER,
    source_document_id TEXT,
    source_document_version INTEGER,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, contract_id, id),
    UNIQUE (tenant_id, contract_id, id, source_document_id, source_document_version),
    UNIQUE (tenant_id, source_document_id, source_document_version),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts (tenant_id, id),
    FOREIGN KEY (tenant_id, source_document_id, source_document_version)
        REFERENCES document_versions (tenant_id, id, version),
    CHECK ((source_document_id IS NULL) = (source_document_version IS NULL)),
    CHECK (status = 'draft' OR (signed_at IS NOT NULL AND activation_on IS NOT NULL)),
    CHECK (issue_to IS NULL OR issue_to > coalesce(issue_from, activation_on))
) STRICT;

CREATE TABLE amendment_changes (
    tenant_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    amendment_id TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('net_days', 'grace_days', 'next_weekday')),
    value INTEGER NOT NULL CHECK (value >= 0),
    document_id TEXT,
    document_version INTEGER,
    span_id TEXT,
    PRIMARY KEY (tenant_id, contract_id, amendment_id, clause_id, field),
    FOREIGN KEY (tenant_id, contract_id, amendment_id)
        REFERENCES amendments (tenant_id, contract_id, id),
    FOREIGN KEY (tenant_id, contract_id, amendment_id, document_id, document_version)
        REFERENCES amendments (tenant_id, contract_id, id, source_document_id, source_document_version),
    FOREIGN KEY (tenant_id, contract_id, clause_id)
        REFERENCES payment_clauses (tenant_id, contract_id, id),
    FOREIGN KEY (tenant_id, document_id, document_version, span_id)
        REFERENCES document_spans (tenant_id, document_id, document_version, id),
    CHECK (field <> 'next_weekday' OR value IN (0, 1)),
    CHECK ((document_id IS NULL) = (document_version IS NULL)
        AND (document_id IS NULL) = (span_id IS NULL))
) STRICT;

CREATE TABLE invoices (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    contract_id TEXT,
    issued_at TEXT,
    stated_due_date TEXT,
    currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    total_minor INTEGER NOT NULL CHECK (total_minor >= 0),
    status TEXT NOT NULL CHECK (status IN ('draft', 'issued', 'void', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, id, currency),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
    FOREIGN KEY (tenant_id, customer_id, contract_id)
        REFERENCES contracts (tenant_id, customer_id, id),
    CHECK (status NOT IN ('issued', 'void') OR issued_at IS NOT NULL),
    CHECK (status <> 'draft' OR issued_at IS NULL)
) STRICT;

CREATE TABLE payments (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('payment', 'credit', 'reversal')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'posted', 'failed')),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    currency TEXT NOT NULL,
    posted_at TEXT,
    reversal_target_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, invoice_id, id),
    FOREIGN KEY (tenant_id, invoice_id, currency) REFERENCES invoices (tenant_id, id, currency),
    FOREIGN KEY (tenant_id, invoice_id, reversal_target_id)
        REFERENCES payments (tenant_id, invoice_id, id),
    CHECK ((kind = 'reversal') = (reversal_target_id IS NOT NULL)),
    CHECK (reversal_target_id IS NULL OR reversal_target_id <> id),
    CHECK ((status = 'posted') = (posted_at IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX payments_one_posted_reversal ON payments (tenant_id, reversal_target_id)
    WHERE kind = 'reversal' AND status = 'posted';

CREATE TABLE disputes (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
    CHECK (closed_at IS NULL OR closed_at >= opened_at)
) STRICT;

CREATE TABLE escalation_policies (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    threshold_days INTEGER NOT NULL CHECK (threshold_days >= 0),
    exclude_disputed INTEGER NOT NULL CHECK (exclude_disputed IN (0, 1)),
    requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
    PRIMARY KEY (tenant_id, id, version)
) STRICT;

CREATE TABLE policy_exclusions (
    tenant_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    subject TEXT NOT NULL CHECK (subject IN ('customer', 'invoice')),
    status TEXT NOT NULL,
    PRIMARY KEY (tenant_id, policy_id, policy_version, subject, status),
    FOREIGN KEY (tenant_id, policy_id, policy_version)
        REFERENCES escalation_policies (tenant_id, id, version),
    CHECK (
        (subject = 'customer' AND status IN ('active', 'on_hold', 'closed'))
        OR (subject = 'invoice' AND status IN ('draft', 'issued', 'void', 'cancelled',
                                               'paid', 'partially_paid', 'unpaid'))
    )
) STRICT;

CREATE TABLE escalation_tickets (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    reason_code TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, invoice_id) REFERENCES invoices (tenant_id, id),
    FOREIGN KEY (tenant_id, policy_id, policy_version)
        REFERENCES escalation_policies (tenant_id, id, version),
    FOREIGN KEY (tenant_id, created_by) REFERENCES users (tenant_id, id)
) STRICT;

CREATE UNIQUE INDEX tickets_one_active_per_invoice ON escalation_tickets (tenant_id, invoice_id)
    WHERE status IN ('open', 'in_progress');

CREATE INDEX invoices_by_customer ON invoices (tenant_id, customer_id, id);
CREATE INDEX payments_by_invoice ON payments (tenant_id, invoice_id, id);
CREATE INDEX disputes_by_invoice ON disputes (tenant_id, invoice_id, id);

PRAGMA user_version = 1;
