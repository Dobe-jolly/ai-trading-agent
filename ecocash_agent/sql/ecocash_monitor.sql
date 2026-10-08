-- ecocash_monitor (PostgreSQL): what the EcoCash monitor records.
-- Applied automatically by `python -m agent monitor` (statements are idempotent).

-- Every link (BANKREG) and unlink (BANKDEREG) request to EcoCash and what came back.
CREATE TABLE IF NOT EXISTS ecocash_link_attempts (
    id BIGSERIAL PRIMARY KEY,
    attempt_key VARCHAR(160) NOT NULL UNIQUE,      -- EcoCash external reference, else host|thread|time
    operation VARCHAR(8) NOT NULL,                 -- LINK | UNLINK
    outcome VARCHAR(16) NOT NULL,                  -- linked | unlinked | failed | no_response
    response_code VARCHAR(32),                     -- EcoCash code: 00, 20503, BCS50646, TP40288, ...
    message TEXT,
    http_status INTEGER,
    channel VARCHAR(32),                           -- NMBCONNECT | IAPPROVE | ...
    via VARCHAR(32),                               -- USSD | VBS (branch staff) | IAPPROVE (account opening)
    staff_user VARCHAR(128),
    mobile_number VARCHAR(32),
    account_number VARCHAR(64),
    id_number VARCHAR(32),
    currency VARCHAR(16),
    first_name VARCHAR(128),
    last_name VARCHAR(128),
    ecocash_name VARCHAR(256),                     -- wallet holder name EcoCash returns on success
    ecocash_transaction_id VARCHAR(128),
    external_reference VARCHAR(64),
    requested_at TIMESTAMP NOT NULL,               -- local time, as ecocashreg.service logs it
    responded_at TIMESTAMP,
    host VARCHAR(64),
    thread VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_link_attempts_requested ON ecocash_link_attempts (requested_at);
CREATE INDEX IF NOT EXISTS idx_link_attempts_mobile ON ecocash_link_attempts (RIGHT(regexp_replace(mobile_number, '\D', '', 'g'), 9));
CREATE INDEX IF NOT EXISTS idx_link_attempts_account ON ecocash_link_attempts (ltrim(regexp_replace(account_number, '\D', '', 'g'), '0'));
CREATE INDEX IF NOT EXISTS idx_link_attempts_id ON ecocash_link_attempts (upper(regexp_replace(id_number, '[^A-Za-z0-9]', '', 'g')));

-- The Java monitor's table (failed links only). Still written, so existing reports keep working.
CREATE TABLE IF NOT EXISTS failed_ecocash_registrations (
    id BIGSERIAL PRIMARY KEY,
    mobile_number VARCHAR(32) NOT NULL,
    error_message TEXT NOT NULL,
    id_number VARCHAR(32) NOT NULL,
    currency VARCHAR(16) NOT NULL,
    first_name VARCHAR(128) NOT NULL,
    last_name VARCHAR(128) NOT NULL,
    account_number VARCHAR(64) NOT NULL,
    failure_timestamp TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
