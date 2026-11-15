-- SPDX-License-Identifier: Apache-2.0
-- Copyright (c) 2026 Cynthia Schomp

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    keyword TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    booking_url TEXT NOT NULL,
    strict_public_passed INTEGER NOT NULL DEFAULT 0,
    human_approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL CHECK (stage IN ('stage1_repo', 'stage2_social')),
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CONSUMED')),
    approval_token_hash TEXT,
    approved_by TEXT,
    approved_via TEXT CHECK (approved_via IS NULL OR approved_via IN ('interactive_tty', 'dashboard_ui')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS channel_deliveries (
    delivery_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('fb_page', 'ig_pro', 'threads')),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'STAGED', 'CONTAINER_CREATED', 'PUBLISHED', 'FAILED')),
    payload_json TEXT NOT NULL,
    container_id TEXT,
    published_id TEXT,
    reply_published_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, channel)
);

CREATE TABLE IF NOT EXISTS inbound_comments (
    event_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    commenter_id TEXT NOT NULL,
    comment_text TEXT NOT NULL,
    matched_keyword TEXT NOT NULL,
    public_reply_id TEXT,
    dm_message_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(platform, comment_id),
    UNIQUE(campaign_id, platform, commenter_id)
);

CREATE TABLE IF NOT EXISTS token_health (
    subject_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    app_mode TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT,
    last_refreshed_at TEXT,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
