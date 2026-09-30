-- 节点基础信息
CREATE TABLE IF NOT EXISTS proxies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    type         TEXT,
    provider     TEXT,
    group_name   TEXT,
    country      TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proxies_group ON proxies(group_name);
CREATE INDEX IF NOT EXISTS idx_proxies_country ON proxies(country);

-- 原始探测记录
CREATE TABLE IF NOT EXISTS probes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_id   INTEGER NOT NULL,
    ts         INTEGER NOT NULL,       -- unix ms
    success    INTEGER NOT NULL,        -- 0/1
    delay_ms   INTEGER,                 -- 失败时为 NULL
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_probes_proxy_ts ON probes(proxy_id, ts);
CREATE INDEX IF NOT EXISTS idx_probes_ts ON probes(ts);

-- 5 分钟聚合
CREATE TABLE IF NOT EXISTS aggregates_5min (
    proxy_id        INTEGER NOT NULL,
    bucket_ts       INTEGER NOT NULL,    -- 桶起始 unix ms
    avg_delay       REAL,
    min_delay       INTEGER,
    max_delay       INTEGER,
    success_count   INTEGER NOT NULL,
    total_count     INTEGER NOT NULL,
    PRIMARY KEY (proxy_id, bucket_ts)
);
CREATE INDEX IF NOT EXISTS idx_5min_ts ON aggregates_5min(bucket_ts);

-- 1 小时聚合
CREATE TABLE IF NOT EXISTS aggregates_1h (
    proxy_id        INTEGER NOT NULL,
    bucket_ts       INTEGER NOT NULL,
    avg_delay       REAL,
    min_delay       INTEGER,
    max_delay       INTEGER,
    success_count   INTEGER NOT NULL,
    total_count     INTEGER NOT NULL,
    PRIMARY KEY (proxy_id, bucket_ts)
);
CREATE INDEX IF NOT EXISTS idx_1h_ts ON aggregates_1h(bucket_ts);

-- 管理员
CREATE TABLE IF NOT EXISTS admins (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    created_at     INTEGER NOT NULL
);

-- 已撤销的 JWT（jti 黑名单），用户主动登出或管理员强制下线
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti         TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    expires_at  INTEGER NOT NULL,    -- unix ms
    revoked_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revoked_expires ON revoked_tokens(expires_at);

-- 自定义节点（CRUD 入口，独立于 mihomo 订阅）
CREATE TABLE IF NOT EXISTS custom_proxies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL,
    config_yaml   TEXT NOT NULL,    -- mihomo clash 格式单节点 yaml
    source_uri    TEXT,             -- 原始 URI（可选）
    group_name    TEXT,
    country       TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    note          TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_custom_proxies_group ON custom_proxies(group_name);

-- 标签表（多对多）
CREATE TABLE IF NOT EXISTS tags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    color        TEXT NOT NULL DEFAULT '#58a6ff',
    created_at   INTEGER NOT NULL
);

-- 节点-标签关联表
CREATE TABLE IF NOT EXISTS proxy_tags (
    proxy_id     INTEGER NOT NULL,
    tag_id       INTEGER NOT NULL,
    PRIMARY KEY (proxy_id, tag_id),
    FOREIGN KEY (proxy_id) REFERENCES custom_proxies(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)   REFERENCES tags(id)         ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_proxy_tags_tag ON proxy_tags(tag_id);

-- 外部订阅源（多源管理 + 定时同步 + 流量统计）
CREATE TABLE IF NOT EXISTS external_subscriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    url                 TEXT NOT NULL,
    user_agent          TEXT,
    fmt                 TEXT NOT NULL DEFAULT 'auto',
    enabled             INTEGER NOT NULL DEFAULT 1,

    -- 同步计划
    sync_mode           TEXT NOT NULL DEFAULT 'interval',  -- interval / daily / weekly / off
    sync_interval_min   INTEGER NOT NULL DEFAULT 360,     -- interval 模式：每 N 分钟
    sync_time           TEXT NOT NULL DEFAULT '03:00',    -- daily/weekly 模式：HH:MM
    sync_weekday        INTEGER NOT NULL DEFAULT 0,        -- weekly 模式：0=周一 .. 6=周日
    default_tag_ids     TEXT,                              -- JSON 数组
    name_filter_regex   TEXT,                              -- 同步时按正则过滤节点名（匹配的排除）

    -- 同步状态
    last_sync_ts        INTEGER,
    last_sync_status    TEXT,
    last_sync_error     TEXT,
    last_sync_node_count INTEGER,

    -- 流量信息
    traffic_upload      INTEGER,
    traffic_download    INTEGER,
    traffic_total       INTEGER,
    traffic_expire_ts   INTEGER,

    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_subs_enabled ON external_subscriptions(enabled);