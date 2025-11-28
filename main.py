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
from urllib.parse import urlparse, parse_qs
import websockets
import aiohttp
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
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
        call_session_id = None
        twilio_call_sid = None
        deepgram_connection = None

        try:
            # Log the full path including query parameters
            # The path variable should include query string: /stream?callSessionId=123&twilioCallSid=CAxxx
            logger.info(f"New WebSocket connection from {websocket.remote_address}")
            logger.info(f"Full path received: {path}")

            # Parse query parameters from the WebSocket URL path
            # Twilio sends parameters as query string in the path
            # Handle both cases: path with or without query string
            if '?' in path:
                parsed_path = urlparse(f"http://dummy{path}")
            else:
                # If no query string in path, try to get from request headers
                parsed_path = urlparse(f"http://dummy{path}")

            query_params = parse_qs(parsed_path.query)
            logger.info(f"Query parameters extracted: {query_params}")

            # Extract parameters (parse_qs returns lists, so get first element)
            call_session_id = query_params.get('callSessionId', [None])[0]
            twilio_call_sid = query_params.get('twilioCallSid', [None])[0]

            # Also check for alternative parameter names
            if not call_session_id:
                call_session_id = query_params.get('callSession', [None])[0]
            if not twilio_call_sid:
                twilio_call_sid = query_params.get('callSid', [None])[0]

            logger.info(f"Parsed parameters from URL - Call session ID: {call_session_id}, Twilio SID: {twilio_call_sid}")

            # Twilio sends a 'connected' event message first with Parameter values
            # The <Parameter> elements from TwiML are sent in the 'connected' event payload
            try:
                initial_message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                if isinstance(initial_message, str):
                    try:
                        params = json.loads(initial_message)
                        event_type = params.get('event')
                        logger.info(f"Received initial message from Twilio: event={event_type}")
                        # Log the full payload to see what Twilio actually sends
                        try:
                            logger.info(f"Full message payload: {json.dumps(params, indent=2)}")
                        except Exception as e:
                            logger.warning(f"Could not serialize payload: {e}")
                            logger.info(f"Payload keys: {list(params.keys()) if isinstance(params, dict) else 'Not a dict'}")

                        # Twilio sends 'connected' event first
                        if event_type == 'connected':
                            logger.info("Twilio Media Stream connected")
                            # According to Twilio docs, <Parameter> elements are sent as query params in URL
                            # But if not in URL, they might be in the message payload
                            # Check all possible locations in the payload

                            # Check for parameters in various possible locations
                            if 'params' in params:
                                call_session_id = call_session_id or params['params'].get('callSessionId')
                                twilio_call_sid = twilio_call_sid or params['params'].get('twilioCallSid')

                            # Check direct fields
                            call_session_id = call_session_id or params.get('callSessionId') or params.get('callSession')
                            twilio_call_sid = twilio_call_sid or params.get('twilioCallSid') or params.get('callSid')

                            # Check in protocol object if it exists
                            if 'protocol' in params and isinstance(params['protocol'], dict):
                                call_session_id = call_session_id or params['protocol'].get('callSessionId')
                                twilio_call_sid = twilio_call_sid or params['protocol'].get('twilioCallSid')

                            # Check in streamSid or other fields
                            if 'streamSid' in params:
                                logger.info(f"Stream SID: {params.get('streamSid')}")

                            logger.info(f"After parsing connected event - Call session ID: {call_session_id}, Twilio SID: {twilio_call_sid}")

                            # If still no call_session_id, don't close yet - wait for 'start' event
                            if not call_session_id:
                                logger.warning("No call_session_id in connected event, will check 'start' event")
                        else:
                            # If it's not a connected event, try to extract parameters anyway
                            call_session_id = call_session_id or params.get('callSessionId') or params.get('callSession')
                            twilio_call_sid = twilio_call_sid or params.get('twilioCallSid') or params.get('callSid')
                    except json.JSONDecodeError as e:
                        logger.warning(f"Initial message is not JSON: {initial_message[:100]}, error: {e}")
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for initial 'connected' message from Twilio")

            # Don't close connection if we don't have call_session_id yet
            # Continue processing messages - parameters might come in 'start' event
            if not call_session_id:
                logger.warning("No call_session_id found yet, continuing to process messages")
                logger.warning(f"Path was: {path}, Query params: {query_params}")
                # Continue - we'll check for call_session_id in subsequent messages
            else:
                logger.info(f"Processing stream for call session: {call_session_id}")

            # Create Deepgram live connection
            # For Deepgram SDK 3.2.7, the API is: deepgram.listen.websocket.v("1")
            try:
                # Check what methods are available on listen object
                logger.debug(f"Available methods on listen: {dir(self.deepgram.listen)}")
                deepgram_connection = self.deepgram.listen.websocket.v("1")
            except AttributeError as e:
                logger.error(f"Deepgram API error - 'websocket' not found: {e}")
                logger.error(f"Available attributes on listen object: {[attr for attr in dir(self.deepgram.listen) if not attr.startswith('_')]}")
                # Try alternative API
                try:
                    # Maybe it's 'live' instead of 'websocket'
                    deepgram_connection = self.deepgram.listen.live.v("1")
                    logger.info("Successfully created Deepgram connection using 'live' API")
                except Exception as e2:
                    logger.error(f"Failed to create Deepgram connection: {e2}")
                    await websocket.close(code=5001, reason="Failed to initialize transcription")
                    return
            except Exception as e:
                logger.error(f"Failed to create Deepgram connection: {e}")
                await websocket.close(code=5001, reason="Failed to initialize transcription")
                return

            # Set up Deepgram event handlers
            # Use a mutable reference for call_session_id since it might be set later
            call_session_id_ref = [call_session_id]  # Use list for mutability

            def transcript_handler(*args, **kwargs):
                current_id = call_session_id_ref[0]
                if current_id:
                    self.on_deepgram_transcript(*args, call_session_id=current_id, **kwargs)
                else:
                    logger.warning("Received transcript but no call_session_id yet")

            deepgram_connection.on(LiveTranscriptionEvents.Open, self.on_deepgram_open)
            deepgram_connection.on(LiveTranscriptionEvents.Transcript, transcript_handler)
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

            # Store connection info
            connection_info = {
                'websocket': websocket,
                'deepgram': deepgram_connection,
                'twilio_call_sid': twilio_call_sid,
                'call_session_id_ref': call_session_id_ref
            }

            if call_session_id:
                self.active_streams[call_session_id] = connection_info

            # Forward audio from Twilio to Deepgram
            # Also watch for 'start' event which may contain parameters
            async for message in websocket:
                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        event = data.get('event')

                        # Check for 'start' event which contains stream metadata
                        if event == 'start':
                            logger.info(f"Received 'start' event: {json.dumps(data, indent=2)}")
                            # Parameters might be in the 'start' event
                            if not call_session_id_ref[0]:
                                # Try to extract from start event
                                start_data = data.get('start', {})
                                call_session_id_ref[0] = start_data.get('callSessionId') or start_data.get('callSession') or data.get('callSessionId') or data.get('callSession')
                                twilio_call_sid = twilio_call_sid or start_data.get('twilioCallSid') or start_data.get('callSid') or data.get('twilioCallSid') or data.get('callSid')

                                if call_session_id_ref[0]:
                                    logger.info(f"Found call_session_id in 'start' event: {call_session_id_ref[0]}")
                                    # Update connection info
                                    connection_info['twilio_call_sid'] = twilio_call_sid
                                    self.active_streams[call_session_id_ref[0]] = connection_info
                                    logger.info(f"Processing stream for call session: {call_session_id_ref[0]}")
                                else:
                                    logger.warning("Still no call_session_id found in 'start' event")

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
    # Note: The path parameter in handle_twilio_stream should include query parameters
    # If not, we'll extract them from the connected event message
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
