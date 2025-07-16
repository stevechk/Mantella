import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from src.config.config_loader import ConfigLoader
from src.http.routes.routeable import routeable
from src.stt import Transcriber
from src import utils
from src.telemetry.telemetry import create_span, add_global_attribute

'''
    NOTE: This route is being deprecated. 
    Player mic input is now handled within game_manager.py's player_input() function
'''

class stt_route(routeable):
    """Route that can be called to get a transcribe from the mic

    Args:
        routeable (_type_): _description_

    Returns:
        _type_: _description_
    """
    PREFIX: str = "mantella_" 
    KEY_REQUESTTYPE: str = PREFIX + "request_type"
    KEY_REQUESTTYPE_TTS: str = PREFIX + "tts"
    KEY_INPUT_NAMESINCONVERSATION: str = PREFIX + "names_in_conversation"

    KEY_REPLYTYPE: str = PREFIX + "reply_type"
    KEY_TRANSCRIBE: str = PREFIX + "transcribe"

    def __init__(self, config: ConfigLoader, stt_secret_key_file: str, secret_key_file: str, show_debug_messages: bool = False) -> None:
        super().__init__(config, show_debug_messages)
        self.__stt: Transcriber | None = None
        self.__stt_secret_key_file = stt_secret_key_file
        self.__secret_key_file = secret_key_file

    @utils.time_it
    def _setup_route(self):
        if not self.__stt:
            self.__stt = Transcriber(self._config, self.__stt_secret_key_file, self.__secret_key_file)

    @utils.time_it
    def add_route_to_server(self, app: FastAPI):
        @app.post("/stt")
        async def stt(request: Request):
            # Create parent span for the STT HTTP request
            with create_span("stt_http_request") as span:
                # Add request metadata
                span.set_attribute("http.method", "POST")
                span.set_attribute("http.url", "/stt")
                span.set_attribute("http.request_id", str(id(request)))
                
                # Add client information if available
                client_host = request.client.host if request.client else "unknown"
                span.set_attribute("http.client_host", client_host)
                
                try:
                    if not self._can_route_be_used():
                        error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
                        logging.error(error_message)
                        span.set_attribute("request.status", "error")
                        span.set_attribute("error.message", error_message)
                        return self.error_message(error_message)
                    
                    if not self.__stt:
                        error_message = "STT/Whisper setup failed. There is most likely an issue with the config.ini."
                        logging.error(error_message)
                        span.set_attribute("request.status", "error")
                        span.set_attribute("error.message", error_message)
                        return self.error_message(error_message)
                    
                    received_json: dict[str, Any] | None = await request.json()
                    
                    if received_json:
                        span.set_attribute("request.has_json", True)
                        span.set_attribute("request.json_size", len(json.dumps(received_json)))
                        
                        if self._show_debug_messages:
                            logging.log(self._log_level_http_in, json.dumps(received_json, indent=4))
                        
                        if received_json[self.KEY_REQUESTTYPE] == self.KEY_REQUESTTYPE_TTS:
                            span.set_attribute("request.type", "tts")
                            
                            names: list[str] = received_json[self.KEY_INPUT_NAMESINCONVERSATION]
                            names_in_conversation = ', '.join(names)
                            
                            span.set_attribute("stt.names_in_conversation", names_in_conversation)
                            span.set_attribute("stt.names_count", len(names))
                            
                            with create_span("speech_recognition") as stt_span:
                                transcribed_text = self.__stt.recognize_input(names_in_conversation)
                                stt_span.set_attribute("stt.recognition_completed", True)
                                stt_span.set_attribute("stt.transcribed_length", len(transcribed_text) if isinstance(transcribed_text, str) else 0)
                            
                            if isinstance(transcribed_text, str):
                                span.set_attribute("request.handled", "tts_success")
                                return self.construct_return_json(transcribed_text)
                            else:
                                span.set_attribute("request.handled", "tts_failed")
                                span.set_attribute("error.message", "Transcription failed")
                        else:
                            span.set_attribute("request.handled", "unknown_request_type")
                            span.set_attribute("error.message", f"Unknown request type: {received_json.get(self.KEY_REQUESTTYPE, 'missing')}")
                    else:
                        span.set_attribute("request.has_json", False)
                        span.set_attribute("request.handled", "invalid_json")
                        span.set_attribute("error.message", "Invalid JSON in request")
                    
                    span.set_attribute("request.status", "success")
                    return self.construct_return_json("*Complete gibberish*")
                    
                except Exception as e:
                    # Record any exceptions in the span
                    span.record_exception(e)
                    span.set_attribute("request.status", "error")
                    span.set_attribute("error.type", type(e).__name__)
                    span.set_attribute("error.message", str(e))
                    
                    logging.error(f"Error processing STT request: {e}")
                    return self.error_message(f"Internal server error: {str(e)}")
    
    @utils.time_it
    def construct_return_json(self, transcribe: str) -> dict:
        reply = {
            self.KEY_REPLYTYPE: self.KEY_REQUESTTYPE_TTS,
            self.KEY_TRANSCRIBE: transcribe,
        }
        if self._show_debug_messages:
            logging.log(self._log_level_http_out, json.dumps(reply, indent=4))
        return reply