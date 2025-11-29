# Media Stream Service

A standalone service that bridges Twilio Media Streams with Deepgram for real-time transcription and forwards transcripts to a Laravel application.

## Overview

This service:
- Accepts WebSocket connections from Twilio Media Streams
- Forwards audio to Deepgram for real-time transcription
- Sends transcript chunks to your Laravel application via HTTP POST

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- Deepgram API key ([Get one here](https://console.deepgram.com/))
- Laravel API URL and authentication token

### Installation

1. **Clone or download this service**

```bash
cd media-stream-service
```

2. **Configure environment variables**

```bash
cp env.example .env
nano .env
```

Update the following variables:
- `DEEPGRAM_API_KEY` - Your Deepgram API key
- `LARAVEL_API_URL` - Full URL to your Laravel application
- `LARAVEL_API_TOKEN` - API token for Laravel authentication

3. **Start the service**

```bash
chmod +x start.sh
./start.sh start
```

## Usage

### Basic Commands

```bash
# Start the service
./start.sh start

# Stop the service
./start.sh stop

# Restart the service
./start.sh restart

# View logs
./start.sh logs

# Check status
./start.sh status
```

### SSL Setup

For production deployment with SSL:

```bash
# Setup SSL certificates (requires domain name)
./start.sh setup-ssl yourdomain.com your-email@example.com
```

After obtaining certificates, update `nginx/conf.d/default.conf` with your domain name, then restart the service.

## Configuration

### Environment Variables

See `env.example` for all available configuration options.

### WebSocket Endpoint

The service accepts WebSocket connections on:
- **Path**: `/stream`
- **Port**: `8080` (internal) or `443` (via Nginx with SSL)

### Laravel Integration

Configure your Laravel application's `.env`:

```env
MEDIA_STREAM_URL=wss://your-media-stream-domain.com/stream
```

## Deployment

For detailed deployment instructions on Digital Ocean, see [DEPLOYMENT.md](./DEPLOYMENT.md).

## Architecture

```
Twilio Call → Media Stream → This Service → Deepgram → Transcripts → Laravel API
```

1. Twilio sends audio via WebSocket to this service
2. Service forwards audio to Deepgram for transcription
3. Deepgram returns transcript chunks
4. Service sends transcripts to Laravel API endpoint

## Troubleshooting

### Service won't start

- Check Docker is running: `docker ps`
- Verify environment variables are set: `cat .env`
- Check logs: `./start.sh logs`

### WebSocket connection fails

- Verify firewall allows ports 80 and 443
- Check SSL certificate is valid
- Ensure domain DNS is configured correctly

### Transcripts not reaching Laravel

- Verify `LARAVEL_API_URL` is correct and accessible
- Check `LARAVEL_API_TOKEN` is valid
- Test Laravel API endpoint manually

## License

[Your License Here]



