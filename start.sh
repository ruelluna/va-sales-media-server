#!/bin/bash

# Media Stream Service Startup Script
# This script helps with starting and managing the media stream service

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Functions
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if .env file exists
if [ ! -f .env ]; then
    print_error ".env file not found!"
    print_info "Please copy env.example to .env and configure it:"
    echo "  cp env.example .env"
    echo "  nano .env"
    exit 1
fi

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    print_error "Docker is not installed!"
    exit 1
fi

# Check if Docker Compose is installed
if ! docker compose version &> /dev/null; then
    print_error "Docker Compose is not installed!"
    exit 1
fi

# Function to start the service
start_service() {
    print_info "Starting Media Stream Service..."
    docker compose up -d --build

    if [ $? -eq 0 ]; then
        print_info "Service started successfully!"
        print_info "View logs with: docker compose logs -f"
        print_info "Check status with: docker compose ps"
    else
        print_error "Failed to start service!"
        exit 1
    fi
}

# Function to stop the service
stop_service() {
    print_info "Stopping Media Stream Service..."
    docker compose down
    print_info "Service stopped."
}

# Function to restart the service
restart_service() {
    print_info "Restarting Media Stream Service..."
    docker compose restart
    print_info "Service restarted."
}

# Function to view logs
view_logs() {
    docker compose logs -f
}

# Function to check status
check_status() {
    print_info "Service Status:"
    docker compose ps

    print_info "\nContainer Logs (last 50 lines):"
    docker compose logs --tail=50
}

# Function to setup SSL certificates
setup_ssl() {
    print_info "Setting up SSL certificates with Let's Encrypt..."

    if [ -z "$1" ]; then
        print_error "Please provide your domain name!"
        echo "Usage: $0 setup-ssl yourdomain.com"
        exit 1
    fi

    DOMAIN=$1
    EMAIL=${2:-admin@$DOMAIN}

    print_info "Domain: $DOMAIN"
    print_info "Email: $EMAIL"

    # Create certbot directories
    mkdir -p certbot/conf
    mkdir -p certbot/www

    # Stop nginx temporarily
    docker compose stop nginx 2>/dev/null || true

    # Get certificate
    print_info "Obtaining SSL certificate..."
    docker run --rm -it \
        -v "$SCRIPT_DIR/certbot/conf:/etc/letsencrypt" \
        -v "$SCRIPT_DIR/certbot/www:/var/www/certbot" \
        -p 80:80 \
        certbot/certbot certonly --standalone \
        -d "$DOMAIN" \
        --email "$EMAIL" \
        --agree-tos \
        --non-interactive

    if [ $? -eq 0 ]; then
        print_info "SSL certificate obtained successfully!"
        print_info "Update nginx/conf.d/default.conf with your domain name"
        print_info "Then restart the service: $0 restart"
    else
        print_error "Failed to obtain SSL certificate!"
        exit 1
    fi
}

# Main script logic
case "${1:-start}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    logs)
        view_logs
        ;;
    status)
        check_status
        ;;
    setup-ssl)
        setup_ssl "$2" "$3"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|logs|status|setup-ssl [domain] [email]}"
        echo ""
        echo "Commands:"
        echo "  start       - Start the media stream service"
        echo "  stop        - Stop the media stream service"
        echo "  restart     - Restart the media stream service"
        echo "  logs        - View service logs"
        echo "  status      - Check service status"
        echo "  setup-ssl   - Setup SSL certificates (requires domain)"
        exit 1
        ;;
esac
