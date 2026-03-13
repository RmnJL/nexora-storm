#!/bin/bash
"""
STORM + 3x-UI Setup Script

محیط تولید را راه‌اندازی کن

Usage:
  chmod +x setup_3xui.sh
  ./setup_3xui.sh
"""

set -e

echo "🚀 STORM + 3x-UI Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check prerequisites
echo -e "${YELLOW}📋 Checking prerequisites...${NC}"

if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker not found${NC}"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo -e "${RED}❌ Docker Compose not found${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Docker & Docker Compose found${NC}"

# Create required directories
echo -e "${YELLOW}📁 Creating directories...${NC}"

mkdir -p /var/log/storm
mkdir -p /etc/storm
mkdir -p ./3xui-config
mkdir -p ./monitoring

chmod 755 /var/log/storm

echo -e "${GREEN}✅ Directories created${NC}"

# Environment setup
echo -e "${YELLOW}⚙️  Setting up environment...${NC}"

cat > .env << 'EOF'
# STORM Configuration
ROLE=local
RESOLVERS=8.8.8.8,1.1.1.1,9.9.9.9
LISTEN_PORT=1443
LISTEN_HOST=0.0.0.0

# DNS Zone
DNS_ZONE=t1.phonexpress.ir
DNS_PORT=53

# Upstream Configuration
UPSTREAM_HOST=127.0.0.1
UPSTREAM_PORT=10000
UPSTREAM_PROTOCOL=socks5

# Database
POSTGRES_USER=storm
POSTGRES_PASSWORD=changeme123
POSTGRES_DB=storm_users

# Logging
LOG_LEVEL=INFO

# 3x-UI
3XUI_ADMIN_USER=admin
3XUI_ADMIN_PASS=admin123
EOF

echo -e "${GREEN}✅ Environment configured${NC}"

# Build Docker images
echo -e "${YELLOW}🔨 Building Docker images...${NC}"

docker-compose build --no-cache

echo -e "${GREEN}✅ Images built${NC}"

# Start services
echo -e "${YELLOW}🚀 Starting services...${NC}"

docker-compose up -d

echo -e "${GREEN}✅ Services started${NC}"

# Wait for services to be ready
echo -e "${YELLOW}⏳ Waiting for services to be ready...${NC}"

sleep 10

# Check services
echo -e "${YELLOW}🔍 Verifying services...${NC}"

echo -n "  3x-UI Dashboard: "
if curl -sf http://localhost:54321 > /dev/null; then
    echo -e "${GREEN}✅${NC}"
else
    echo -e "${RED}❌${NC}"
fi

echo -n "  Redis: "
if docker-compose exec -T redis redis-cli ping > /dev/null 2>&1; then
    echo -e "${GREEN}✅${NC}"
else
    echo -e "${RED}❌${NC}"
fi

echo -n "  PostgreSQL: "
if docker-compose exec -T postgres pg_isready -U storm > /dev/null 2>&1; then
    echo -e "${GREEN}✅${NC}"
else
    echo -e "${RED}❌${NC}"
fi

echo -n "  STORM Inside: "
if docker-compose logs storm-inside 2>&1 | grep -q "STORM client listening"; then
    echo -e "${GREEN}✅${NC}"
else
    echo -e "${RED}❌${NC}"
fi

# Create demo users
echo -e "${YELLOW}👥 Creating demo users...${NC}"

docker-compose exec -T postgres psql -U storm -d storm_users << 'EOSQL'
INSERT INTO users (uuid, email, username, tier, traffic_limit_gb, status) VALUES
    ('550e8400-e29b-41d4-a716-446655440000', 'user1@example.com', 'user1', 'pro', 100, 'active'),
    ('6ba7b810-9dad-11d1-80b4-00c04fd430c8', 'user2@example.com', 'user2', 'free', 10, 'active'),
    ('6ba7b844-9dad-11d1-80b4-00c04fd430c8', 'user3@example.com', 'user3', 'pro', 100, 'active')
ON CONFLICT DO NOTHING;
EOSQL

echo -e "${GREEN}✅ Demo users created${NC}"

# Display access information
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ STORM + 3x-UI Setup Complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

echo ""
echo -e "${YELLOW}📊 Access Information:${NC}"
echo ""
echo "3x-UI Dashboard:"
echo "  URL: http://localhost:54321"
echo "  User: admin"
echo "  Pass: admin123"
echo ""
echo "STORM Inside Gateway:"
echo "  Listen: 0.0.0.0:1443"
echo "  Protocol: TCP tunnel endpoint for Xray SOCKS outbound"
echo ""
echo "STORM Outside Gateway:"
echo "  Listen: 0.0.0.0:53"
echo "  Protocol: DNS (UDP)"
echo ""
echo "Monitoring:"
echo "  Prometheus: http://localhost:9090"
echo "  Grafana: http://localhost:3000 (admin/admin123)"
echo ""
echo "Database:"
echo "  Host: localhost"
echo "  Port: 5432"
echo "  User: storm"
echo "  Pass: changeme123"
echo "  DB: storm_users"
echo ""
echo -e "${YELLOW}Demo Users:${NC}"
echo "  UUID 1: 550e8400-e29b-41d4-a716-446655440000"
echo "  UUID 2: 6ba7b810-9dad-11d1-80b4-00c04fd430c8"
echo "  UUID 3: 6ba7b844-9dad-11d1-80b4-00c04fd430c8"
echo ""
echo -e "${YELLOW}🔧 Useful Commands:${NC}"
echo ""
echo "View logs:"
echo "  docker-compose logs -f storm-inside"
echo "  docker-compose logs -f storm-outside"
echo ""
echo "Get VLESS config for user:"
cat << 'VLESS'
  vless://550e8400-e29b-41d4-a716-446655440000@localhost:443?
    encryption=none&
    security=tls&
    sni=example.com&
    type=tcp
VLESS
echo ""
echo "Add to 3x-UI:"
echo "  1. Open http://localhost:54321"
echo "  2. Create new inbound → VLESS"
echo "  3. Set port 443 for user inbound (STORM runs on 1443)"
echo "  4. Add users with above UUIDs"
echo ""
echo "Test connection:"
echo "  # Linux/Mac"
echo "  curl -x socks5://550e8400-e29b-41d4-a716-446655440000:@localhost:443 http://example.com"
echo ""

echo -e "${GREEN}✨ Setup complete! Happy tunneling!${NC}"
echo ""
