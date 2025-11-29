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
        call_session_id_ref = [None]  # Mutable reference, initialized early

        # Get the current event loop for scheduling async tasks from sync callbacks
        event_loop = asyncio.get_event_loop()

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
            call_session_id_ref[0] = query_params.get('callSessionId', [None])[0]
            twilio_call_sid = query_params.get('twilioCallSid', [None])[0]

            # Also check for alternative parameter names
            if not call_session_id_ref[0]:
                call_session_id_ref[0] = query_params.get('callSession', [None])[0]
            if not twilio_call_sid:
                twilio_call_sid = query_params.get('callSid', [None])[0]

            logger.info(f"Parsed parameters from URL - Call session ID: {call_session_id_ref[0]}, Twilio SID: {twilio_call_sid}")

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
                                call_session_id_ref[0] = call_session_id_ref[0] or params['params'].get('callSessionId')
                                twilio_call_sid = twilio_call_sid or params['params'].get('twilioCallSid')

                            # Check direct fields
                            call_session_id_ref[0] = call_session_id_ref[0] or params.get('callSessionId') or params.get('callSession')
                            twilio_call_sid = twilio_call_sid or params.get('twilioCallSid') or params.get('callSid')

                            # Check in protocol object if it exists
                            if 'protocol' in params and isinstance(params['protocol'], dict):
                                call_session_id_ref[0] = call_session_id_ref[0] or params['protocol'].get('callSessionId')
                                twilio_call_sid = twilio_call_sid or params['protocol'].get('twilioCallSid')

                            # Check in streamSid or other fields
                            if 'streamSid' in params:
                                logger.info(f"Stream SID: {params.get('streamSid')}")

                            logger.info(f"After parsing connected event - Call session ID: {call_session_id_ref[0]}, Twilio SID: {twilio_call_sid}")

                            # If still no call_session_id, don't close yet - wait for 'start' event
                            if not call_session_id_ref[0]:
                                logger.warning("No call_session_id in connected event, will check 'start' event")
                        else:
                            # If it's not a connected event, try to extract parameters anyway
                            call_session_id_ref[0] = call_session_id_ref[0] or params.get('callSessionId') or params.get('callSession')
                            twilio_call_sid = twilio_call_sid or params.get('twilioCallSid') or params.get('callSid')
                    except json.JSONDecodeError as e:
                        logger.warning(f"Initial message is not JSON: {initial_message[:100]}, error: {e}")
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for initial 'connected' message from Twilio")

            # Don't close connection if we don't have call_session_id yet
            # Continue processing messages - parameters might come in 'start' event
            if not call_session_id_ref[0]:
                logger.warning("No call_session_id found yet, continuing to process messages")
                logger.warning(f"Path was: {path}, Query params: {query_params}")
                # Continue - we'll check for call_session_id in subsequent messages
            else:
                logger.info(f"Processing stream for call session: {call_session_id_ref[0]}")

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
                    await websocket.close(code=1011, reason="Failed to initialize transcription")
                    return
            except Exception as e:
                logger.error(f"Failed to create Deepgram connection: {e}")
                await websocket.close(code=1011, reason="Failed to initialize transcription")
                return

            # Set up Deepgram event handlers
            # call_session_id_ref is already initialized as a mutable reference

            def transcript_handler(*args, **kwargs):
                logger.info(f"Transcript handler called! args count: {len(args)}, kwargs: {list(kwargs.keys())}")
                current_id = call_session_id_ref[0]
                if current_id:
                    logger.info(f"Scheduling async task for transcript processing, call_session_id: {current_id}")
                    # Deepgram SDK calls handlers from its own thread, so we need to use run_coroutine_threadsafe
                    # to schedule the async function on the event loop from a different thread
                    # Pass both args and kwargs to preserve the result object
                    coro = self.on_deepgram_transcript(*args, call_session_id=current_id, **kwargs)
                    try:
                        # Schedule the coroutine on the event loop from this thread
                        asyncio.run_coroutine_threadsafe(coro, event_loop)
                    except Exception as e:
                        logger.error(f"Error scheduling transcript task: {e}", exc_info=True)
                else:
                    logger.warning("Received transcript but no call_session_id yet")

            # Register event handlers
            deepgram_connection.on(LiveTranscriptionEvents.Open, self.on_deepgram_open)
            deepgram_connection.on(LiveTranscriptionEvents.Transcript, transcript_handler)
            deepgram_connection.on(LiveTranscriptionEvents.Error, self.on_deepgram_error)
            deepgram_connection.on(LiveTranscriptionEvents.Close, self.on_deepgram_close)

            # Log that handlers are registered
            logger.info("Deepgram event handlers registered: Open, Transcript, Error, Close")

            # Start Deepgram connection
            # Twilio sends mu-law audio at 8000 Hz, converted to PCM16
            # Deepgram should auto-detect the format, but we'll try specifying it
            try:
                # Try with sample_rate and encoding first
                try:
                    options = LiveOptions(
                        model="nova-2",
                        language="en-US",
                        smart_format=True,
                        interim_results=True,
                        utterance_end_ms=1000,
                        vad_events=True,
                        diarize=True,  # Enable speaker diarization
                        sample_rate=8000,
                        encoding="linear16",
                    )
                    logger.info("Created LiveOptions with speaker diarization enabled")
                except TypeError:
                    # If those parameters aren't supported, use basic options
                    logger.warning("sample_rate/encoding not supported in LiveOptions, using basic options")
                    try:
                        options = LiveOptions(
                            model="nova-2",
                            language="en-US",
                            smart_format=True,
                            interim_results=True,
                            utterance_end_ms=1000,
                            vad_events=True,
                            diarize=True,  # Enable speaker diarization
                        )
                        logger.info("Created LiveOptions with speaker diarization (basic options)")
                    except TypeError:
                        # Fallback if diarize is not supported
                        logger.warning("diarize not supported in LiveOptions, using basic options without diarization")
                        options = LiveOptions(
                            model="nova-2",
                            language="en-US",
                            smart_format=True,
                            interim_results=True,
                            utterance_end_ms=1000,
                            vad_events=True,
                        )
                        logger.info("Created LiveOptions with basic options (no diarization)")

                if not deepgram_connection.start(options):
                    logger.error("Failed to start Deepgram connection")
                    await websocket.close(code=1011, reason="Failed to start transcription")
                    return

                # Log diarization status
                diarize_enabled = getattr(options, 'diarize', False) if hasattr(options, 'diarize') else False
                logger.info(f"Deepgram connection started successfully - Speaker diarization: {'ENABLED' if diarize_enabled else 'DISABLED'}")
                if not diarize_enabled:
                    logger.warning("WARNING: Speaker diarization is DISABLED. Only one speaker will be detected!")
            except Exception as e:
                logger.error(f"Error starting Deepgram: {e}", exc_info=True)
                await websocket.close(code=1011, reason="Failed to start transcription")
                return

            # Store connection info
            # Track speaker mapping: Deepgram speaker IDs -> 'va' or 'prospect'
            # We'll use a simple heuristic: first speaker is usually the VA (caller)
            speaker_mapping = {}  # Maps Deepgram speaker ID to 'va' or 'prospect'
            next_speaker_index = 0  # Track which speaker index to assign next

            connection_info = {
                'websocket': websocket,
                'deepgram': deepgram_connection,
                'twilio_call_sid': twilio_call_sid,
                'call_session_id_ref': call_session_id_ref,
                'media_count': 0,  # Track number of media packets received
                'speaker_mapping': speaker_mapping,
                'next_speaker_index': next_speaker_index,
            }

            if call_session_id_ref[0]:
                self.active_streams[call_session_id_ref[0]] = connection_info

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

                                # Check customParameters first (where Twilio sends <Parameter> values)
                                custom_params = start_data.get('customParameters', {})
                                call_session_id_ref[0] = (
                                    custom_params.get('callSessionId') or
                                    custom_params.get('callSession') or
                                    start_data.get('callSessionId') or
                                    start_data.get('callSession') or
                                    data.get('callSessionId') or
                                    data.get('callSession')
                                )

                                # Extract Twilio call SID from start event
                                twilio_call_sid = (
                                    twilio_call_sid or
                                    start_data.get('callSid') or
                                    data.get('callSid') or
                                    data.get('twilioCallSid')
                                )

                                if call_session_id_ref[0]:
                                    logger.info(f"Found call_session_id in 'start' event: {call_session_id_ref[0]}")
                                    logger.info(f"Twilio Call SID: {twilio_call_sid}")
                                    # Update connection info
                                    connection_info['twilio_call_sid'] = twilio_call_sid
                                    self.active_streams[call_session_id_ref[0]] = connection_info
                                    logger.info(f"Processing stream for call session: {call_session_id_ref[0]}")
                                else:
                                    logger.warning("Still no call_session_id found in 'start' event")
                                    logger.warning(f"Checked customParameters: {custom_params}")
                                    logger.warning(f"Checked start_data keys: {list(start_data.keys())}")

                        if event == 'media':
                            # Decode mu-law audio from Twilio
                            connection_info['media_count'] = connection_info.get('media_count', 0) + 1
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
                                        # Log every 100th packet to avoid spam
                                        if connection_info['media_count'] % 100 == 0:
                                            logger.info(f"Sent {connection_info['media_count']} audio packets to Deepgram (latest: {len(pcm_audio)} bytes)")
                                    else:
                                        logger.warning("Deepgram connection not available when trying to send audio")
                                except Exception as e:
                                    logger.error(f"Error processing audio data: {e}", exc_info=True)
                                    continue
                            else:
                                logger.debug("Media event received but no payload")
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON received: {e}")
                        continue
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        continue

        except websockets.exceptions.ConnectionClosedOK:
            logger.info(f"Twilio connection closed gracefully for call {call_session_id_ref[0]}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Twilio connection closed unexpectedly for call {call_session_id_ref[0]}: {e.code} - {e.reason}")
        except Exception as e:
            logger.error(f"Error handling stream for call {call_session_id_ref[0]}: {e}", exc_info=True)
        finally:
            final_call_session_id = call_session_id_ref[0]

            if deepgram_connection:
                try:
                    deepgram_connection.finish()
                    logger.info(f"Deepgram connection finished for call {final_call_session_id}")
                except Exception as e:
                    logger.error(f"Error closing Deepgram connection for call {final_call_session_id}: {e}")

            if final_call_session_id and final_call_session_id in self.active_streams:
                self.active_streams.pop(final_call_session_id, None)
                logger.info(f"Cleaned up stream for call session {final_call_session_id}")

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
        logger.info(f"Deepgram connection opened - args: {args}, kwargs: {kwargs}")

    async def on_deepgram_transcript(self, *args, call_session_id=None, **kwargs):
        """Handle Deepgram transcript events"""
        logger.info(f"Deepgram transcript event received for call_session_id: {call_session_id}")

        # The transcript result is passed in kwargs['result'], not args[0]
        # args[0] is the LiveClient object
        transcript_result = kwargs.get('result') if 'result' in kwargs else (args[0] if args else None)

        if not transcript_result:
            logger.warning("Deepgram transcript result is None")
            return

        try:
            # Log the result structure for debugging (only log first few times to avoid spam)
            if not hasattr(self, '_transcript_log_count'):
                self._transcript_log_count = 0
            self._transcript_log_count += 1

            if self._transcript_log_count <= 3:
                logger.info(f"Deepgram transcript_result type: {type(transcript_result)}")
                logger.info(f"Deepgram transcript_result attributes: {[attr for attr in dir(transcript_result) if not attr.startswith('_')]}")

            # Extract transcript text from the result object
            sentence = None

            # Try to extract from channel.alternatives (standard Deepgram structure)
            if hasattr(transcript_result, 'channel') and hasattr(transcript_result.channel, 'alternatives'):
                if transcript_result.channel.alternatives and len(transcript_result.channel.alternatives) > 0:
                    sentence = transcript_result.channel.alternatives[0].transcript
                    logger.info(f"Extracted transcript from channel.alternatives[0].transcript: {sentence[:100] if sentence else 'None'}")

            # Try alternative paths
            if not sentence:
                if hasattr(transcript_result, 'alternatives') and transcript_result.alternatives:
                    sentence = transcript_result.alternatives[0].transcript if hasattr(transcript_result.alternatives[0], 'transcript') else None
                    logger.info(f"Extracted transcript from alternatives[0]: {sentence[:100] if sentence else 'None'}")

            if not sentence:
                if hasattr(transcript_result, 'transcript'):
                    sentence = transcript_result.transcript
                    logger.info(f"Extracted transcript from transcript attribute: {sentence[:100] if sentence else 'None'}")

            if not sentence or not sentence.strip():
                logger.warning("No transcript text found or empty transcript - skipping")
                logger.warning(f"Result structure: type={type(transcript_result)}, has channel={hasattr(transcript_result, 'channel')}")
                if hasattr(transcript_result, 'channel'):
                    logger.warning(f"Channel structure: {dir(transcript_result.channel)}")
                return

            # Determine speaker using Deepgram speaker diarization
            # Get connection info to access speaker mapping
            connection_info = None
            if call_session_id and call_session_id in self.active_streams:
                connection_info = self.active_streams[call_session_id]

            speaker = 'prospect'  # Default fallback

            # Extract speaker ID from Deepgram result
            speaker_id = None
            try:
                # Deepgram provides speaker info in words array or alternatives
                if hasattr(transcript_result, 'channel') and hasattr(transcript_result.channel, 'alternatives'):
                    if transcript_result.channel.alternatives and len(transcript_result.channel.alternatives) > 0:
                        alt = transcript_result.channel.alternatives[0]
                        # Check if words have speaker information
                        if hasattr(alt, 'words') and alt.words and len(alt.words) > 0:
                            # Get speaker from first word (all words in an utterance should have same speaker)
                            first_word = alt.words[0]
                            if hasattr(first_word, 'speaker'):
                                speaker_id = getattr(first_word, 'speaker', None)
                            elif hasattr(first_word, 'speaker_label'):
                                speaker_id = getattr(first_word, 'speaker_label', None)
                            # Try alternative attribute names
                            elif hasattr(first_word, 'speaker_id'):
                                speaker_id = getattr(first_word, 'speaker_id', None)
                            elif hasattr(first_word, 'speakerId'):
                                speaker_id = getattr(first_word, 'speakerId', None)

                # Alternative: check if speaker is at channel level
                if not speaker_id and hasattr(transcript_result, 'channel'):
                    if hasattr(transcript_result.channel, 'speaker'):
                        speaker_id = getattr(transcript_result.channel, 'speaker', None)
                    elif hasattr(transcript_result.channel, 'speaker_label'):
                        speaker_id = getattr(transcript_result.channel, 'speaker_label', None)
                    elif hasattr(transcript_result.channel, 'speaker_id'):
                        speaker_id = getattr(transcript_result.channel, 'speaker_id', None)

                # Alternative: check at result level
                if not speaker_id:
                    if hasattr(transcript_result, 'speaker'):
                        speaker_id = getattr(transcript_result, 'speaker', None)
                    elif hasattr(transcript_result, 'speaker_label'):
                        speaker_id = getattr(transcript_result, 'speaker_label', None)

                # Log speaker detection for debugging
                if speaker_id is not None:
                    logger.info(f"Detected speaker ID from Deepgram: {speaker_id} (type: {type(speaker_id)})")
                else:
                    logger.warning("No speaker ID detected in Deepgram result - diarization may not be working")
                    # Log the structure for debugging
                    if hasattr(transcript_result, 'channel') and hasattr(transcript_result.channel, 'alternatives'):
                        if transcript_result.channel.alternatives and len(transcript_result.channel.alternatives) > 0:
                            alt = transcript_result.channel.alternatives[0]
                            logger.warning(f"Alternative structure: {dir(alt)}")
                            if hasattr(alt, 'words') and alt.words and len(alt.words) > 0:
                                logger.warning(f"First word structure: {dir(alt.words[0])}")

                # Map Deepgram speaker ID to 'va' or 'prospect'
                if speaker_id is not None and connection_info:
                    speaker_mapping = connection_info.get('speaker_mapping', {})
                    next_speaker_index = connection_info.get('next_speaker_index', 0)

                    # Convert speaker_id to string for consistent mapping
                    speaker_key = str(speaker_id)

                    # If we haven't seen this speaker before, assign it
                    if speaker_key not in speaker_mapping:
                        # First speaker (index 0) is typically the VA (caller)
                        # Second speaker (index 1) is typically the prospect
                        if next_speaker_index == 0:
                            speaker_mapping[speaker_key] = 'va'
                            logger.info(f"Mapping new speaker {speaker_key} to 'va' (first speaker detected)")
                        else:
                            speaker_mapping[speaker_key] = 'prospect'
                            logger.info(f"Mapping new speaker {speaker_key} to 'prospect' (second speaker detected)")

                        connection_info['speaker_mapping'] = speaker_mapping
                        connection_info['next_speaker_index'] = next_speaker_index + 1
                        self.active_streams[call_session_id] = connection_info

                        # Log current mapping state
                        logger.info(f"Current speaker mappings for call {call_session_id}: {speaker_mapping}")

                    # Get the mapped speaker
                    speaker = speaker_mapping.get(speaker_key, 'prospect')
                    logger.info(f"Speaker {speaker_key} mapped to '{speaker}' for transcript: {sentence[:50]}...")
                elif speaker_id is None:
                    logger.warning("No speaker ID found in Deepgram result, using default 'prospect'")
                    logger.warning("This may indicate that speaker diarization is not enabled or not working correctly")
            except Exception as e:
                logger.error(f"Error extracting speaker information: {e}", exc_info=True)
                # Fallback to default
                speaker = 'prospect'
                logger.warning(f"Using fallback speaker 'prospect' due to error")

            # Calculate timestamp (relative to call start)
            # Check multiple possible locations for timestamp
            timestamp = 0.0
            try:
                # Try to get start time from the result object
                if hasattr(transcript_result, 'start'):
                    start_attr = getattr(transcript_result, 'start', None)
                    # Check if it's a numeric value, not a method
                    if start_attr is not None and not callable(start_attr):
                        timestamp = float(start_attr)
                elif hasattr(transcript_result, 'start_time'):
                    timestamp = float(getattr(transcript_result, 'start_time', 0.0))
                elif hasattr(transcript_result, 'message_ts'):
                    timestamp = float(getattr(transcript_result, 'message_ts', 0.0))
                elif hasattr(transcript_result, 'channel') and hasattr(transcript_result.channel, 'alternatives'):
                    if transcript_result.channel.alternatives and len(transcript_result.channel.alternatives) > 0:
                        alt = transcript_result.channel.alternatives[0]
                        if hasattr(alt, 'start'):
                            alt_start = getattr(alt, 'start', None)
                            if alt_start is not None and not callable(alt_start):
                                timestamp = float(alt_start)
                        elif hasattr(alt, 'words') and alt.words and len(alt.words) > 0:
                            # Use the start time of the first word
                            first_word = alt.words[0]
                            if hasattr(first_word, 'start'):
                                word_start = getattr(first_word, 'start', None)
                                if word_start is not None:
                                    timestamp = float(word_start)
            except (ValueError, TypeError, AttributeError) as e:
                logger.debug(f"Could not extract timestamp: {e}, using 0.0")
                timestamp = 0.0

            logger.info(f"Processing transcript: speaker={speaker}, text={sentence[:50]}..., timestamp={timestamp}")

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
        # Ensure timestamp is a numeric value, not a method or object
        timestamp_value = float(timestamp) if timestamp is not None else 0.0

        payload = {
            'call_session_id': str(call_session_id),  # Ensure it's a string
            'speaker': str(speaker),
            'text': str(text),
            'timestamp': timestamp_value,
        }

        logger.info(f"Sending transcript to Laravel: call_session_id={payload['call_session_id']}, speaker={payload['speaker']}, text_length={len(payload['text'])}, timestamp={payload['timestamp']}, text_preview={text[:50]}...")

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
                        logger.info(f"Successfully sent transcript to Laravel: speaker={speaker}, text={text[:50]}...")

                        # Track speaker distribution for this call session
                        if call_session_id and call_session_id in self.active_streams:
                            connection_info = self.active_streams[call_session_id]
                            if 'speaker_stats' not in connection_info:
                                connection_info['speaker_stats'] = {'va': 0, 'prospect': 0, 'system': 0}
                            connection_info['speaker_stats'][speaker] = connection_info['speaker_stats'].get(speaker, 0) + 1
                            self.active_streams[call_session_id] = connection_info

                            # Log stats every 10 transcripts
                            total = sum(connection_info['speaker_stats'].values())
                            if total % 10 == 0:
                                stats = connection_info['speaker_stats']
                                va_count = stats.get('va', 0)
                                prospect_count = stats.get('prospect', 0)
                                logger.info(f"Speaker distribution for call {call_session_id} (total={total}): VA={va_count}, Prospect={prospect_count}, System={stats.get('system', 0)}")

                                # Warn if we're only seeing one speaker after many transcripts
                                if total >= 20:
                                    if va_count == 0:
                                        logger.warning(f"WARNING: No VA transcripts detected after {total} transcripts. Check if speaker diarization is working correctly.")
                                    elif prospect_count == 0:
                                        logger.warning(f"WARNING: No prospect transcripts detected after {total} transcripts. Check if speaker diarization is working correctly.")
                                    elif va_count > 0 and prospect_count > 0:
                                        logger.info(f"✓ Both speakers detected: VA ({va_count}) and Prospect ({prospect_count})")
                    elif response.status == 401:
                        response_text = await response.text()
                        logger.error(f"Authentication failed when sending transcript: HTTP {response.status} - {response_text}")
                        logger.error(f"Check if LARAVEL_API_TOKEN is set correctly")
                    elif response.status == 422:
                        response_text = await response.text()
                        logger.error(f"Validation failed when sending transcript: HTTP {response.status} - {response_text}")
                        logger.error(f"Payload was: call_session_id={call_session_id}, speaker={speaker}, text_length={len(text)}, timestamp={timestamp}")
                    else:
                        response_text = await response.text()
                        logger.error(f"Failed to send transcript: HTTP {response.status} - {response_text}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout sending transcript to Laravel for call {call_session_id}")
        except aiohttp.ClientError as e:
            logger.error(f"Error sending to Laravel: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending to Laravel: {e}", exc_info=True)

    def on_deepgram_error(self, *args, **kwargs):
        """Handle Deepgram errors"""
        logger.error(f"Deepgram error event received: args={args}, kwargs={kwargs}")
        if args:
            logger.error(f"Deepgram error details: {args[0]}")
        # Log all arguments for debugging
        for i, arg in enumerate(args):
            logger.error(f"Deepgram error arg[{i}]: {arg} (type: {type(arg)})")

    def on_deepgram_close(self, *args, **kwargs):
        """Handle Deepgram connection close"""
        logger.info(f"Deepgram connection closed - args: {args}, kwargs: {kwargs}")


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
