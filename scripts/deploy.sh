#!/usr/bin/env bash
# ==============================================================
# AI Guardian System — 云端部署脚本
# 目标：阿里云 ECS (8.130.183.132)
# 用法：
#   1. 设置环境变量：
#      export ECS_HOST=8.130.183.132
#      export ECS_USER=root
#      export DASHSCOPE_API_KEY=sk-xxxxxxxx
#   2. 运行：bash scripts/deploy.sh
# ==============================================================

set -euo pipefail

# ---------- 配置 ----------
ECS_HOST="${ECS_HOST:-8.130.183.132}"
ECS_USER="${ECS_USER:-root}"
REMOTE_DIR="/opt/ai-guardian"
DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-}"
BACKEND_PORT=8000

if [ -z "$DASHSCOPE_API_KEY" ]; then
    echo "WARNING: DASHSCOPE_API_KEY not set, backend will run in stub mode"
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "========================================="
echo "  AI Guardian System — Deploy to ECS"
echo "  Host: $ECS_HOST"
echo "  Dir:  $REMOTE_DIR"
echo "========================================="

# ---------- 1. 打包后端代码 ----------
echo ""
echo "[1/5] Packaging backend..."
TMPTAR=$(mktemp /tmp/ai-guardian-XXXXX.tar.gz)
tar -czf "$TMPTAR" \
    -C "$PROJECT_ROOT" \
    backend/ \
    pyproject.toml
echo "  -> $TMPTAR ($(du -h "$TMPTAR" | cut -f1))"

# ---------- 2. 上传到 ECS ----------
echo ""
echo "[2/5] Uploading to $ECS_HOST:$REMOTE_DIR ..."
ssh "$ECS_USER@$ECS_HOST" "mkdir -p $REMOTE_DIR"
scp "$TMPTAR" "$ECS_USER@$ECS_HOST:$REMOTE_DIR/deploy.tar.gz"
rm -f "$TMPTAR"

# ---------- 3. 远程解压 & 安装依赖 ----------
echo ""
echo "[3/5] Installing on remote..."
ssh "$ECS_USER@$ECS_HOST" bash <<REMOTE_SCRIPT
set -euo pipefail
cd $REMOTE_DIR

# 解压
tar -xzf deploy.tar.gz
rm -f deploy.tar.gz

# Python 虚拟环境
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# 安装依赖
pip install --upgrade pip -q
pip install fastapi uvicorn httpx python-multipart -q

# 安装 AI 依赖（如果有 key）
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
    pip install dashscope openai -q
    echo "  AI deps installed"
fi

echo "  Dependencies installed"
REMOTE_SCRIPT

# ---------- 4. 创建 systemd service ----------
echo ""
echo "[4/5] Setting up systemd service..."
ssh "$ECS_USER@$ECS_HOST" bash <<REMOTE_SCRIPT
set -euo pipefail

cat > /etc/systemd/system/ai-guardian.service <<EOF
[Unit]
Description=AI Guardian Backend
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_DIR
Environment=DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY}
Environment=LOG_LEVEL=INFO
ExecStart=$REMOTE_DIR/venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port $BACKEND_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ai-guardian
systemctl restart ai-guardian

echo "  Service started"
REMOTE_SCRIPT

# ---------- 5. 验证 ----------
echo ""
echo "[5/5] Verifying health endpoint..."
sleep 2

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://$ECS_HOST:$BACKEND_PORT/api/v1/health" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
    echo "  Health check PASSED (HTTP 200)"
    curl -s "http://$ECS_HOST:$BACKEND_PORT/api/v1/health" | python3 -m json.tool 2>/dev/null || true
else
    echo "  Health check returned HTTP $HTTP_CODE"
    echo "  Check logs: ssh $ECS_USER@$ECS_HOST journalctl -u ai-guardian -f"
fi

echo ""
echo "========================================="
echo "  Deployment complete!"
echo "  Backend: http://$ECS_HOST:$BACKEND_PORT"
echo "  Health:  http://$ECS_HOST:$BACKEND_PORT/api/v1/health"
echo "  Logs:    ssh $ECS_USER@$ECS_HOST journalctl -u ai-guardian -f"
echo "========================================="
