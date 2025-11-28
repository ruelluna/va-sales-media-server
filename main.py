#!/usr/bin/env python3
"""
Twilio Media Stream to Deepgram Bridge Service

This service accepts Twilio Media Stream WebSocket connections,
forwards audio to Deepgram for real-time transcription,
and sends transcript chunks to Laravel via HTTP POST.
"""

import asyncio
import json
import os
import base64
import signal
import sys
import logging
import websockets
import aiohttp
from deepgram import DeepgramClient, PrerecordedOptions, LiveOptions, LiveTranscriptionEvents
from deepgram.clients.live import LiveClient
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')
LARAVEL_API_URL = os.getenv('LARAVEL_API_URL', 'http://laravel:8000')
LARAVEL_API_TOKEN = os.getenv('LARAVEL_API_TOKEN')
MEDIA_STREAM_PORT = int(os.getenv('MEDIA_STREAM_PORT', 8080))

# Validate required environment variables
if not DEEPGRAM_API_KEY:
    logger.error("DEEPGRAM_API_KEY environment variable is required")
    sys.exit(1)

if not LARAVEL_API_URL:
    logger.error("LARAVEL_API_URL environment variable is required")
    sys.exit(1)


class MediaStreamHandler:
    def __init__(self):
        if not DEEPGRAM_API_KEY:
            raise ValueError("DEEPGRAM_API_KEY is required")
        self.deepgram = DeepgramClient(DEEPGRAM_API_KEY)
        self.active_streams = {}
        self.server = None

    async def handle_twilio_stream(self, websocket, path):
        """Handle incoming Twilio Media Stream WebSocket connection"""
        # Only accept connections on /stream path
        if path != '/stream':
            logger.warning(f"Rejected connection to invalid path: {path}")
            await websocket.close(code=4004, reason="Invalid path")
            return

        call_session_id = None
        twilio_call_sid = None
        deepgram_connection = None

        try:
            logger.info(f"New WebSocket connection from {websocket.remote_address}")

            # Receive initial connection parameters from Twilio
            try:
                initial_message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                if isinstance(initial_message, str):
                    params = json.loads(initial_message)
                    call_session_id = params.get('callSessionId')
                    twilio_call_sid = params.get('twilioCallSid')
                    logger.info(f"Call session initialized: {call_session_id}, Twilio SID: {twilio_call_sid}")
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for initial message from Twilio")
                await websocket.close(code=4008, reason="Timeout waiting for initial message")
                return
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in initial message: {e}")
                await websocket.close(code=4007, reason="Invalid message format")
                return

            if not call_session_id:
                logger.warning("No call_session_id provided in initial message")
                await websocket.close(code=4003, reason="Missing call_session_id")
                return

            # Create Deepgram live connection
            try:
                deepgram_connection = self.deepgram.listen.websocket.v("1")
            except Exception as e:
                logger.error(f"Failed to create Deepgram connection: {e}")
                await websocket.close(code=5001, reason="Failed to initialize transcription")
                return

            # Set up Deepgram event handlers
            deepgram_connection.on(LiveTranscriptionEvents.Open, self.on_deepgram_open)
            deepgram_connection.on(LiveTranscriptionEvents.Transcript, lambda *args: self.on_deepgram_transcript(*args, call_session_id=call_session_id))
            deepgram_connection.on(LiveTranscriptionEvents.Error, self.on_deepgram_error)
            deepgram_connection.on(LiveTranscriptionEvents.Close, self.on_deepgram_close)

            # Start Deepgram connection
            try:
                if not deepgram_connection.start(LiveOptions(
                    model="nova-2",
                    language="en-US",
                    smart_format=True,
                    interim_results=True,
                    utterance_end_ms=1000,
                    vad_events=True,
                )):
                    logger.error("Failed to start Deepgram connection")
                    await websocket.close(code=5002, reason="Failed to start transcription")
                    return
            except Exception as e:
                logger.error(f"Error starting Deepgram: {e}")
                await websocket.close(code=5002, reason="Failed to start transcription")
                return

            self.active_streams[call_session_id] = {
                'websocket': websocket,
                'deepgram': deepgram_connection,
                'twilio_call_sid': twilio_call_sid
            }

            # Forward audio from Twilio to Deepgram
            async for message in websocket:
                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        event = data.get('event')

                        if event == 'media':
                            # Decode mu-law audio from Twilio
                            payload = data.get('media', {}).get('payload')
                            if payload:
                                try:
                                    # Convert base64 mu-law to PCM
                                    audio_data = base64.b64decode(payload)
                                    # Deepgram expects PCM16 audio
                                    pcm_audio = self.mulaw_to_pcm(audio_data)
                                    # Send to Deepgram
                                    if deepgram_connection:
                                        deepgram_connection.send(pcm_audio)
                                except Exception as e:
                                    logger.error(f"Error processing audio data: {e}")
                                    continue
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON received: {e}")
                        continue
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        continue

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Twilio connection closed for call {call_session_id}")
        except Exception as e:
            logger.error(f"Error handling stream: {e}", exc_info=True)
        finally:
            if deepgram_connection:
                try:
                    deepgram_connection.finish()
                except Exception as e:
                    logger.error(f"Error closing Deepgram connection: {e}")
            if call_session_id and call_session_id in self.active_streams:
                self.active_streams.pop(call_session_id, None)
                logger.info(f"Cleaned up stream for call session {call_session_id}")

    def mulaw_to_pcm(self, mulaw_data):
        """Convert mu-law audio to PCM16"""
        # Mu-law to linear conversion
        mulaw_table = [
            -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
            -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
            -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
            -11900, -11388, -10876, -10364, -9852, -9340, -8828, -8316,
            -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
            -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
            -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
            -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
            -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
            -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
            -876, -844, -812, -780, -748, -716, -684, -652,
            -620, -588, -556, -524, -492, -460, -428, -396,
            -372, -356, -340, -324, -308, -292, -276, -260,
            -244, -228, -212, -196, -180, -164, -148, -132,
            -120, -112, -104, -96, -88, -80, -72, -64,
            -56, -48, -40, -32, -24, -16, -8, 0,
            32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
            23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
            15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
            11900, 11388, 10876, 10364, 9852, 9340, 8828, 8316,
            7932, 7676, 7420, 7164, 6908, 6652, 6396, 6140,
            5884, 5628, 5372, 5116, 4860, 4604, 4348, 4092,
            3900, 3772, 3644, 3516, 3388, 3260, 3132, 3004,
            2876, 2748, 2620, 2492, 2364, 2236, 2108, 1980,
            1884, 1820, 1756, 1692, 1628, 1564, 1500, 1436,
            1372, 1308, 1244, 1180, 1116, 1052, 988, 924,
            876, 844, 812, 780, 748, 716, 684, 652,
            620, 588, 556, 524, 492, 460, 428, 396,
            372, 356, 340, 324, 308, 292, 276, 260,
            244, 228, 212, 196, 180, 164, 148, 132,
            120, 112, 104, 96, 88, 80, 72, 64,
            56, 48, 40, 32, 24, 16, 8, 0
        ]

        pcm_data = bytearray()
        for byte in mulaw_data:
            pcm_value = mulaw_table[byte]
            # Convert to 16-bit signed integer (little-endian)
            pcm_data.extend(pcm_value.to_bytes(2, byteorder='little', signed=True))

        return bytes(pcm_data)

    def on_deepgram_open(self, *args, **kwargs):
        """Handle Deepgram connection open"""
        logger.info("Deepgram connection opened")

    async def on_deepgram_transcript(self, *args, call_session_id=None, **kwargs):
        """Handle Deepgram transcript events"""
        if not args or len(args) == 0:
            return

        result = args[0]
        if not result:
            return

        try:
            sentence = result.channel.alternatives[0].transcript if result.channel.alternatives else None
            if not sentence or not sentence.strip():
                return

            # Determine speaker (Deepgram can provide speaker diarization)
            # For now, we'll use a simple heuristic or default to 'prospect'
            speaker = 'prospect'  # Default, can be enhanced with Deepgram speaker diarization
            if hasattr(result, 'channel') and hasattr(result.channel, 'alternatives'):
                # Check if Deepgram provides speaker information
                pass

            # Calculate timestamp (relative to call start)
            timestamp = result.start if hasattr(result, 'start') else 0.0

            # Send to Laravel
            await self.send_to_laravel(call_session_id, speaker, sentence, timestamp)
        except Exception as e:
            logger.error(f"Error processing transcript: {e}", exc_info=True)

    async def send_to_laravel(self, call_session_id, speaker, text, timestamp):
        """Send transcript chunk to Laravel API"""
        if not call_session_id:
            logger.warning("Attempted to send transcript without call_session_id")
            return

        url = f"{LARAVEL_API_URL}/api/media-stream"
        payload = {
            'call_session_id': call_session_id,
            'speaker': speaker,
            'text': text,
            'timestamp': timestamp,
        }

        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        if LARAVEL_API_TOKEN:
            headers['Authorization'] = f'Bearer {LARAVEL_API_TOKEN}'

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status == 201:
                        logger.debug(f"Sent transcript to Laravel: {text[:50]}...")
                    else:
                        response_text = await response.text()
                        logger.warning(f"Failed to send transcript: HTTP {response.status} - {response_text}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout sending transcript to Laravel for call {call_session_id}")
        except aiohttp.ClientError as e:
            logger.error(f"Error sending to Laravel: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending to Laravel: {e}", exc_info=True)

    def on_deepgram_error(self, *args, **kwargs):
        """Handle Deepgram errors"""
        if args:
            logger.error(f"Deepgram error: {args[0]}")

    def on_deepgram_close(self, *args, **kwargs):
        """Handle Deepgram connection close"""
        logger.info("Deepgram connection closed")


async def main():
    """Main entry point"""
    handler = MediaStreamHandler()

    logger.info(f"Starting Media Stream Service on port {MEDIA_STREAM_PORT}...")
    logger.info(f"Laravel API URL: {LARAVEL_API_URL}")
    logger.info(f"Deepgram API configured: {'Yes' if DEEPGRAM_API_KEY else 'No'}")

    # Create WebSocket server
    handler.server = await websockets.serve(
        handler.handle_twilio_stream,
        "0.0.0.0",
        MEDIA_STREAM_PORT,
        ping_interval=20,
        ping_timeout=10,
        close_timeout=10
    )

    logger.info(f"Media Stream Service started successfully on port {MEDIA_STREAM_PORT}")
    logger.info("Waiting for WebSocket connections on /stream path...")

    # Set up signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        handler.server.close()
        for call_session_id in list(handler.active_streams.keys()):
            stream_info = handler.active_streams.get(call_session_id)
            if stream_info and 'deepgram' in stream_info:
                try:
                    stream_info['deepgram'].finish()
                except Exception as e:
                    logger.error(f"Error closing Deepgram connection: {e}")

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        await handler.server.wait_closed()
        logger.info("Server closed gracefully")
    except Exception as e:
        logger.error(f"Error in server: {e}", exc_info=True)
    finally:
        # Clean up remaining connections
        for call_session_id in list(handler.active_streams.keys()):
            stream_info = handler.active_streams.pop(call_session_id, None)
            if stream_info and 'deepgram' in stream_info:
                try:
                    stream_info['deepgram'].finish()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
