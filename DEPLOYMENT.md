# Media Stream Service Deployment Guide for Digital Ocean

This guide will help you deploy the Media Stream Service as a standalone service on Digital Ocean.

## Prerequisites

- Digital Ocean account
- A domain name pointing to your Digital Ocean droplet
- Deepgram API key ([Get one here](https://console.deepgram.com/))
- Laravel API URL and authentication token

## Step 1: Create Digital Ocean Droplet

1. Log in to your Digital Ocean account
2. Click "Create" → "Droplets"
3. Choose an image:
   - **Ubuntu 22.04 LTS** (recommended)
4. Choose a plan:
   - **Minimum**: 1GB RAM / 1 vCPU ($6/month)
   - **Recommended**: 2GB RAM / 1 vCPU ($12/month) for better performance
5. Choose a datacenter region closest to your users
6. Add your SSH key for secure access
7. Click "Create Droplet"

## Step 2: Initial Server Setup

### Connect to your droplet

```bash
ssh root@your_droplet_ip
```

### Update system packages

```bash
apt update && apt upgrade -y
```

### Install Docker and Docker Compose

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Install Docker Compose
apt install docker-compose-plugin -y

# Verify installation
docker --version
docker compose version
```

### Create a non-root user (recommended)

```bash
# Create user
adduser media-stream
usermod -aG docker media-stream

# Switch to new user
su - media-stream
```

## Step 3: Configure Domain and DNS

1. In Digital Ocean, go to **Networking** → **Domains**
2. Add your domain (e.g., `media-stream.yourdomain.com`)
3. Add an A record:
   - **Hostname**: `media-stream` (or `@` for root domain)
   - **Points to**: Your droplet's IP address
   - **TTL**: 3600

Wait for DNS propagation (can take a few minutes to hours).

## Step 4: Deploy the Service

### Clone or upload the service files

```bash
# Create directory
mkdir -p ~/media-stream-service
cd ~/media-stream-service

# If using git, clone your repository
# git clone <your-repo-url> .

# Or upload files via SCP from your local machine:
# scp -r media-stream-service/* user@your_droplet_ip:~/media-stream-service/
```

### Set up environment variables

```bash
# Copy the example environment file
cp env.example .env

# Edit the environment file
nano .env
```

Update the following variables:

```env
DEEPGRAM_API_KEY=your_actual_deepgram_api_key
LARAVEL_API_URL=https://your-laravel-domain.com
LARAVEL_API_TOKEN=your_laravel_api_token
MEDIA_STREAM_PORT=8080
```

### Update Nginx configuration with your domain

```bash
nano nginx/conf.d/default.conf
```

Replace `YOUR_DOMAIN` with your actual domain name in the SSL certificate paths:

```nginx
ssl_certificate /etc/letsencrypt/live/media-stream.yourdomain.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/media-stream.yourdomain.com/privkey.pem;
```

## Step 5: Set Up SSL Certificate with Let's Encrypt

### Install Certbot

```bash
apt install certbot -y
```

### Create directories for certificates

```bash
mkdir -p certbot/conf
mkdir -p certbot/www
```

### Obtain SSL certificate

```bash
# Stop nginx temporarily (if running)
docker compose down

# Get certificate
certbot certonly --standalone \
  -d media-stream.yourdomain.com \
  --email your-email@example.com \
  --agree-tos \
  --non-interactive

# Copy certificates to certbot directory
cp -r /etc/letsencrypt/live/media-stream.yourdomain.com certbot/conf/
cp -r /etc/letsencrypt/archive/media-stream.yourdomain.com certbot/conf/archive/
```

### Set up automatic renewal

```bash
# Create renewal script
cat > /etc/cron.monthly/renew-media-stream-cert.sh << 'EOF'
#!/bin/bash
certbot renew --quiet
docker compose -f /home/media-stream/media-stream-service/docker-compose.yml restart nginx
EOF

chmod +x /etc/cron.monthly/renew-media-stream-cert.sh
```

## Step 6: Configure Firewall

```bash
# Enable UFW firewall
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# Verify firewall status
ufw status
```

## Step 7: Start the Service

```bash
cd ~/media-stream-service

# Build and start containers
docker compose up -d --build

# Check logs
docker compose logs -f

# Verify containers are running
docker compose ps
```

## Step 8: Configure Laravel Application

Update your Laravel application's `.env` file:

```env
MEDIA_STREAM_URL=wss://media-stream.yourdomain.com/stream
```

## Step 9: Verify Deployment

### Test WebSocket connection

You can test the WebSocket connection using a tool like `wscat`:

```bash
# Install wscat
npm install -g wscat

# Test connection
wscat -c wss://media-stream.yourdomain.com/stream
```

### Check service health

```bash
# Health check endpoint
curl https://media-stream.yourdomain.com/health
```

### Monitor logs

```bash
# View media stream service logs
docker compose logs -f media-stream

# View nginx logs
docker compose logs -f nginx
```

## Troubleshooting

### Service won't start

```bash
# Check container status
docker compose ps

# View detailed logs
docker compose logs media-stream

# Check environment variables
docker compose exec media-stream env
```

### SSL certificate issues

```bash
# Verify certificate exists
ls -la certbot/conf/live/YOUR_DOMAIN/

# Test certificate renewal
certbot renew --dry-run

# Check nginx SSL configuration
docker compose exec nginx nginx -t
```

### WebSocket connection fails

1. Verify firewall allows ports 80 and 443
2. Check Nginx logs: `docker compose logs nginx`
3. Verify SSL certificate is valid
4. Check that the domain DNS is properly configured
5. Ensure the `/stream` path is correctly proxied

### Connection to Laravel fails

1. Verify `LARAVEL_API_URL` is correct and accessible
2. Check `LARAVEL_API_TOKEN` is valid
3. Test Laravel API endpoint manually:
   ```bash
   curl -X POST https://your-laravel-domain.com/api/media-stream \
     -H "Authorization: Bearer YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"test": "data"}'
   ```

## Maintenance

### Update the service

```bash
cd ~/media-stream-service

# Pull latest changes (if using git)
git pull

# Rebuild and restart
docker compose up -d --build
```

### Backup certificates

```bash
# Backup certificates
tar -czf certbot-backup-$(date +%Y%m%d).tar.gz certbot/
```

### Monitor resource usage

```bash
# Check container resource usage
docker stats

# Check disk space
df -h

# Check memory usage
free -h
```

## Security Recommendations

1. **Keep system updated**: Regularly run `apt update && apt upgrade`
2. **Use strong passwords**: For any user accounts
3. **Enable fail2ban**: Protect against brute force attacks
   ```bash
   apt install fail2ban -y
   ```
4. **Regular backups**: Backup your certificates and configuration
5. **Monitor logs**: Regularly check logs for suspicious activity
6. **Limit SSH access**: Use SSH keys instead of passwords

## Scaling Considerations

If you need to handle more concurrent connections:

1. **Upgrade Droplet**: Increase RAM and CPU
2. **Load Balancing**: Set up multiple instances behind a load balancer
3. **Monitoring**: Set up monitoring tools (e.g., Digital Ocean Monitoring, Datadog)

## Support

For issues specific to:
- **Deepgram**: Check [Deepgram Documentation](https://developers.deepgram.com/)
- **Docker**: Check [Docker Documentation](https://docs.docker.com/)
- **Nginx**: Check [Nginx Documentation](https://nginx.org/en/docs/)



