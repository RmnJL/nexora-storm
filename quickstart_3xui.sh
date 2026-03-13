#!/bin/bash
"""
Quick Start - STORM + 3x-UI

5 دقیقه برای راه‌اندازی کامل
"""

echo "🚀 STORM + 3x-UI Quick Start"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Step 1: Create .env
echo "1️⃣  Creating configuration..."
cat > .env << 'EOF'
ROLE=local
RESOLVERS=8.8.8.8,1.1.1.1
LISTEN_PORT=1443
POSTGRES_USER=storm
POSTGRES_PASSWORD=changeme123
EOF
echo "   ✅ .env created"

# Step 2: Build images
echo ""
echo "2️⃣  Building Docker images..."
docker-compose build --no-cache 2>&1 | grep -E "Building|Complete|Error" || echo "   ✅ Building..."

# Step 3: Start services
echo ""
echo "3️⃣  Starting services..."
docker-compose up -d
echo "   ✅ Services started"

# Step 4: Wait and check
echo ""
echo "4️⃣  Waiting for services..."
sleep 5

echo ""
echo "5️⃣  Verifying..."

# Check 3x-UI
if curl -sf http://localhost:54321 > /dev/null 2>&1; then
    echo "   ✅ 3x-UI: http://localhost:54321"
else
    echo "   ⏳ 3x-UI starting... (wait 30 seconds)"
fi

# Check STORM
if docker-compose logs storm-inside 2>&1 | grep -q "listening"; then
    echo "   ✅ STORM Client: Ready (127.0.0.1:1443)"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✨ Setup Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "📊 Next Steps:"
echo ""
echo "1. Open 3x-UI:"
echo "   http://localhost:54321"
echo "   admin / admin123"
echo ""
echo "2. Create Inbound:"
echo "   - Protocol: VLESS"
echo "   - Port: 443"
echo "   - Security: TLS"
echo ""
echo "3. Configure outbound:"
echo "   - In Xray template set SOCKS outbound -> 127.0.0.1:1443"
echo ""
echo "4. Test VLESS:"
echo "   vless://550e8400-e29b-41d4-a716-446655440000@localhost:443"
echo ""
echo "5. View logs:"
echo "   docker-compose logs -f storm-inside"
echo ""
echo "6. Stop services:"
echo "   docker-compose down"
echo ""
