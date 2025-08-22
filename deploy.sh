#!/bin/bash

# LuovAin! Creative Workshop - Deployment Script
# Usage: ./deploy.sh [production|development]

set -e

ENVIRONMENT=${1:-development}
APP_DIR="/home/workshop/CreativeWeek"
DB_NAME="creative_workshop"
DB_USER="workshop_user"

echo "🚀 Deploying LuovAin! Creative Workshop Platform"
echo "Environment: $ENVIRONMENT"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Function to print colored output
print_status() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
    exit 1
}

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   print_error "Don't run this script as root. Use a dedicated user account."
fi

# 1. System Updates
print_status "Updating system packages..."
sudo apt update && sudo apt upgrade -y

# 2. Install PostgreSQL
print_status "Installing PostgreSQL 15..."
sudo apt install -y postgresql-15 postgresql-client-15 postgresql-contrib-15
sudo systemctl start postgresql
sudo systemctl enable postgresql

# 3. Create database and user
print_status "Setting up database..."
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;" 2>/dev/null || true
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD 'CHANGE_ME_SECURE_PASSWORD';" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" 2>/dev/null || true
sudo -u postgres psql -c "ALTER USER $DB_USER CREATEDB;" 2>/dev/null || true

# 4. Create application user
print_status "Creating workshop user..."
sudo useradd -m -s /bin/bash workshop 2>/dev/null || true

# 5. Install Python and dependencies
print_status "Installing Python 3.11..."
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip

# 6. Setup application directory
print_status "Setting up application directory..."
sudo mkdir -p $APP_DIR
sudo chown workshop:workshop $APP_DIR

# Switch to workshop user for application setup
print_status "Installing application as workshop user..."
sudo -u workshop bash << 'EOF'
cd /home/workshop/CreativeWeek

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install Python packages
pip install --upgrade pip
pip install -r requirements.txt

# Set correct permissions
chmod +x CreativeTool.py
EOF

# 7. Create environment file template
print_status "Creating environment configuration..."
sudo -u workshop tee $APP_DIR/.env.template > /dev/null << 'EOF'
# OpenAI Configuration
OPENAI_API_KEY=sk-proj-your-gpt5-api-key-here

# Database Configuration  
CSW_DB_URL=postgresql://workshop_user:CHANGE_ME_SECURE_PASSWORD@localhost/creative_workshop

# Model Configuration (optional)
CSW_MODEL=gpt-5
CSW_EMBED_MODEL=text-embedding-3-small

# Flask Configuration (production)
FLASK_ENV=production
FLASK_DEBUG=False
EOF

print_warning "⚠ Please edit $APP_DIR/.env.template and save as $APP_DIR/.env"

# 8. Install Nginx (production only)
if [[ $ENVIRONMENT == "production" ]]; then
    print_status "Installing Nginx..."
    sudo apt install -y nginx
    
    # Create Nginx configuration
    sudo tee /etc/nginx/sites-available/creative-workshop > /dev/null << 'EOF'
server {
    listen 80;
    server_name creative.agicola.fi;
    
    client_max_body_size 10M;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Long timeouts for AI processing
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
        proxy_send_timeout 300;
    }
    
    location /static {
        alias /home/workshop/CreativeWeek/static;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
    
    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
}
EOF
    
    # Enable site
    sudo ln -sf /etc/nginx/sites-available/creative-workshop /etc/nginx/sites-enabled/
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t
    sudo systemctl restart nginx
    sudo systemctl enable nginx
    
    print_status "Nginx configured for production"
fi

# 9. Create systemd service
print_status "Creating systemd service..."
sudo tee /etc/systemd/system/creative-workshop.service > /dev/null << EOF
[Unit]
Description=LuovAin Creative Workshop Flask App
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=workshop
Group=workshop
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/.venv/bin
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn -w 4 -b 127.0.0.1:8000 --timeout 120 --access-logfile - --error-logfile - CreativeTool:app
Restart=always
RestartSec=10
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=creative-workshop

[Install]
WantedBy=multi-user.target
EOF

# 10. Setup log rotation
print_status "Configuring log rotation..."
sudo tee /etc/logrotate.d/creative-workshop > /dev/null << 'EOF'
/var/log/syslog {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    sharedscripts
    postrotate
        systemctl reload rsyslog > /dev/null 2>&1 || true
    endscript
}
EOF

# 11. Setup firewall
if [[ $ENVIRONMENT == "production" ]]; then
    print_status "Configuring firewall..."
    sudo ufw --force enable
    sudo ufw allow ssh
    sudo ufw allow 'Nginx Full'
    sudo ufw deny 8000
    sudo ufw deny 5432
    print_status "Firewall configured"
fi

# 12. Configure PostgreSQL for production
if [[ $ENVIRONMENT == "production" ]]; then
    print_status "Optimizing PostgreSQL for production..."
    
    # Backup original config
    sudo cp /etc/postgresql/15/main/postgresql.conf /etc/postgresql/15/main/postgresql.conf.backup
    
    # Apply production settings
    sudo tee -a /etc/postgresql/15/main/postgresql.conf.local > /dev/null << 'EOF'

# LuovAin Workshop Production Settings
max_connections = 200
shared_buffers = 2GB
effective_cache_size = 6GB
work_mem = 16MB
maintenance_work_mem = 256MB

# Logging for research purposes
log_statement = 'all'
log_duration = on
log_directory = '/var/log/postgresql'
log_filename = 'creative_workshop_%Y%m%d.log'
log_rotation_size = 100MB
log_min_duration_statement = 1000

# Performance
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
EOF
    
    echo "include 'postgresql.conf.local'" | sudo tee -a /etc/postgresql/15/main/postgresql.conf
    
    sudo systemctl restart postgresql
    print_status "PostgreSQL optimized for production"
fi

# 13. Create backup script
print_status "Setting up automated backups..."
sudo -u workshop tee $APP_DIR/backup.sh > /dev/null << 'EOF'
#!/bin/bash
BACKUP_DIR="/home/workshop/backups"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Database backup
pg_dump creative_workshop > "$BACKUP_DIR/db_backup_$DATE.sql"

# Keep only last 7 days of backups
find $BACKUP_DIR -name "db_backup_*.sql" -mtime +7 -delete

echo "Backup completed: $DATE"
EOF

sudo chmod +x $APP_DIR/backup.sh

# Add to crontab
(sudo -u workshop crontab -l 2>/dev/null; echo "0 3 * * * $APP_DIR/backup.sh >> /var/log/creative-workshop-backup.log 2>&1") | sudo -u workshop crontab -

# 14. Final instructions
echo ""
echo "🎉 Deployment completed successfully!"
echo ""
print_warning "IMPORTANT: Complete these manual steps:"
echo "1. Edit $APP_DIR/.env with your actual OpenAI API key and database password"
echo "2. Update the database password in PostgreSQL:"
echo "   sudo -u postgres psql -c \"ALTER USER workshop_user PASSWORD 'your-secure-password';\""
echo "3. Test database connection:"
echo "   sudo -u workshop psql -h localhost -U workshop_user -d creative_workshop"
echo "4. Start the service:"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable creative-workshop"
echo "   sudo systemctl start creative-workshop"
echo "5. Check service status:"
echo "   sudo systemctl status creative-workshop"
echo "6. View logs:"
echo "   sudo journalctl -u creative-workshop -f"
echo ""

if [[ $ENVIRONMENT == "production" ]]; then
    echo "Production-specific:"
    echo "7. Setup SSL certificate (Let's Encrypt recommended):"
    echo "   sudo apt install certbot python3-certbot-nginx"
    echo "   sudo certbot --nginx -d creative.agicola.fi"
    echo "8. Test Nginx configuration:"
    echo "   sudo nginx -t && sudo systemctl reload nginx"
fi

echo ""
print_status "Deployment script completed!"
echo "Your Creative Workshop platform is ready to use! 🚀"