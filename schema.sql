-- STORM User Management Schema

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    uuid UUID UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE,
    username VARCHAR(100),
    password_hash VARCHAR(255),
    tier VARCHAR(20) DEFAULT 'free',
    traffic_limit_gb INT DEFAULT 10,
    traffic_used_gb DECIMAL(10, 2) DEFAULT 0,
    concurrent_limit INT DEFAULT 1,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connection_id VARCHAR(255),
    stream_id INT,
    peer_address INET,
    bytes_in BIGINT DEFAULT 0,
    bytes_out BIGINT DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    status VARCHAR(20) DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS traffic_log (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id INT REFERENCES sessions(id) ON DELETE SET NULL,
    bytes_in BIGINT,
    bytes_out BIGINT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tier VARCHAR(20),
    price DECIMAL(8, 2),
    billing_cycle VARCHAR(20),
    auto_renew BOOLEAN DEFAULT TRUE,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    status VARCHAR(20) DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash VARCHAR(255) UNIQUE,
    name VARCHAR(100),
    last_used TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

-- Indexes for performance
CREATE INDEX idx_users_uuid ON users(uuid);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_status ON sessions(status);
CREATE INDEX idx_traffic_log_user_id ON traffic_log(user_id);
CREATE INDEX idx_traffic_log_timestamp ON traffic_log(timestamp);
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_api_keys_user_id ON api_keys(user_id);

-- Views for analytics
CREATE VIEW user_stats AS
SELECT 
    u.id,
    u.uuid,
    u.email,
    u.tier,
    COUNT(DISTINCT s.id) AS active_sessions,
    SUM(COALESCE(tl.bytes_in, 0)) AS total_bytes_in,
    SUM(COALESCE(tl.bytes_out, 0)) AS total_bytes_out,
    (SUM(COALESCE(tl.bytes_in, 0)) + SUM(COALESCE(tl.bytes_out, 0))) / (1024*1024*1024) AS total_usage_gb,
    MAX(s.started_at) AS last_activity
FROM users u
LEFT JOIN sessions s ON u.id = s.user_id AND s.status = 'active'
LEFT JOIN traffic_log tl ON u.id = tl.user_id
GROUP BY u.id, u.uuid, u.email, u.tier;

-- Function to update user traffic
CREATE OR REPLACE FUNCTION update_user_traffic()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE users 
    SET traffic_used_gb = traffic_used_gb + (NEW.bytes_in + NEW.bytes_out) / (1024.0*1024.0*1024.0),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.user_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to update traffic on log insert
CREATE TRIGGER traffic_log_update
AFTER INSERT ON traffic_log
FOR EACH ROW
EXECUTE FUNCTION update_user_traffic();

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO storm;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO storm;
