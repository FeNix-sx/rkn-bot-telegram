PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    has_trial_used INTEGER NOT NULL DEFAULT 0,
    trial_start TEXT,
    trial_end TEXT,
    xui_email TEXT,
    xui_uuid TEXT,
    subscription_url TEXT,
    warned_48h INTEGER NOT NULL DEFAULT 0,
    warned_24h INTEGER NOT NULL DEFAULT 0,
    paid_until TEXT,
    plan_devices INTEGER NOT NULL DEFAULT 1,
    is_admin INTEGER NOT NULL DEFAULT 0,
    approved_by_tg_id INTEGER,
    vpn_issued_by_tg_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_trial_end ON users(trial_end);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_tg_id INTEGER NOT NULL,
    amount REAL,
    currency TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    proof_file_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_payments_user_tg_id ON payments(user_tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
