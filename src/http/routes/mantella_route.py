import json
import logging
from typing import Any, Hashable

from fastapi import FastAPI, Request
from src.config.config_loader import ConfigLoader
from src.games.fallout4 import fallout4
from src.games.gameable import gameable
from src.games.skyrim import skyrim
from src.output_manager import ChatManager
from src.llm.openai_client import openai_client, function_client
from src.game_manager import GameStateManager
from src.http.routes.routeable import routeable
from src.http.communication_constants import communication_constants as comm_consts
from src.tts.ttsable import ttsable
from src.tts.xvasynth import xvasynth
from src.tts.xtts import xtts
from src.tts.piper import piper
from src import utils
from src.telemetry.telemetry import create_span, add_global_attribute

class mantella_route(routeable):
    """Main route for Mantella conversations

    Args:
        routeable (_type_): _description_
    """
    def __init__(self, config: ConfigLoader, stt_secret_key_file: str, secret_key_file: str, function_llm_secret_key_file: str,language_info: dict[Hashable, str], show_debug_messages: bool = False) -> None:
        super().__init__(config, show_debug_messages)
        self.__language_info: dict[Hashable, str] = language_info
        self.__secret_key_file: str = secret_key_file
        self.__function_llm_secret_key_file: str =function_llm_secret_key_file
        self.__stt_secret_key_file = stt_secret_key_file
        self.__game: GameStateManager | None = None

        # if not self._can_route_be_used():
        #     error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
        #     logging.error(error_message)

    @utils.time_it
    def _setup_route(self):
        if self.__game:
            self.__game.end_conversation({})

        client = openai_client(self._config, self.__secret_key_file)
        function_client_instance = function_client(self._config, self.__function_llm_secret_key_file, self.__secret_key_file) 

        # Determine which game we're running for and select the appropriate character file
        game: gameable
        formatted_game_name = self._config.game.lower().replace(' ', '').replace('_', '')
        if formatted_game_name in ("fallout4", "fallout4vr"):
            game = fallout4(self._config)
        else:
            game = skyrim(self._config)

        tts: ttsable
        if self._config.tts_service == 'xvasynth':
            tts = xvasynth(self._config)
        elif self._config.tts_service == 'xtts':
            tts = xtts(self._config, game)
        if self._config.tts_service == 'piper':
            tts = piper(self._config, game)

        client = openai_client(self._config, self.__secret_key_file)
        
        chat_manager = ChatManager(game, self._config, tts, client, function_client_instance)
        self.__game = GameStateManager(game, chat_manager, self._config, self.__language_info, client, self.__stt_secret_key_file, self.__secret_key_file)

    
    @utils.time_it
    def add_route_to_server(self, app: FastAPI):
        @app.post("/mantella")
        async def mantella(request: Request):
            # Create parent span for the HTTP request
            with create_span("mantella_http_request") as span:
                # Add request metadata
                span.set_attribute("http.method", "POST")
                span.set_attribute("http.url", "/mantella")
                span.set_attribute("http.request_id", str(id(request)))
                                
                logging.debug('Received request')
                
                try:
                    if not self._can_route_be_used():
                        error_message = "MantellaSoftware settings faulty. Please check MantellaSoftware's window or log."
                        logging.error(error_message)
                        span.set_attribute("request.status", "error")
                        span.set_attribute("error.message", error_message)
                        return self.error_message(error_message)
                    
                    if not self.__game:
                        error_message = "Game manager setup failed. There is most likely an issue with the config.ini."
                        logging.error(error_message)
                        span.set_attribute("request.status", "error")
                        span.set_attribute("error.message", error_message)
                        return self.error_message(error_message)
                    
                    reply = {}
                    received_json: dict[str, Any] | None = await request.json()
                    
                    if received_json:
                        logging.debug('Processing request...')
                        logging.log(self._log_level_http_in, json.dumps(received_json, indent=4))
                                                
                        request_type: str = received_json[comm_consts.KEY_REQUESTTYPE]
                        span.set_attribute("request.has_json", True)
                        span.set_attribute("request.json_size", len(json.dumps(received_json)))
                        span.set_attribute("request.type", request_type)
                        
                        # Add game-specific context if available
                        if 'npc_name' in received_json:
                            span.set_attribute("npc.name", received_json['npc_name'])
                        if 'player_name' in received_json:
                            span.set_attribute("player.name", received_json['player_name'])
                        
                        match request_type:
                            case comm_consts.KEY_REQUESTTYPE_INIT:
                                with create_span("init") as conv_span:
                                    # nothing needs to be done for this request aside from self._can_route_be_used() being triggered
                                    logging.debug('Mantella settings initialized')
                                    reply = {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTTYPE_INITCOMPLETED}
                                span.set_attribute("request.handled", "init")
                                
                            case comm_consts.KEY_REQUESTTYPE_STARTCONVERSATION:
                                with create_span("start_conversation") as conv_span:
                                    reply = self.__game.start_conversation(received_json)
                                    conv_span.set_attribute("conversation.started", True)
                                    if 'npc_name' in received_json:
                                        conv_span.set_attribute("npc.name", received_json['npc_name'])
                                span.set_attribute("request.handled", "start_conversation")
                                
                            case comm_consts.KEY_REQUESTTYPE_CONTINUECONVERSATION:
                                with create_span("continue_conversation") as conv_span:
                                    reply = self.__game.continue_conversation(received_json)
                                    conv_span.set_attribute("conversation.continued", True)
                                    if 'npc_name' in received_json:
                                        conv_span.set_attribute("npc.name", received_json['npc_name'])
                                span.set_attribute("request.handled", "continue_conversation")
                                #logging.log(23, f"reply to MantellaMod is {reply}")
                                
                            case comm_consts.KEY_REQUESTTYPE_PLAYERINPUT:
                                with create_span("player_input") as input_span:
                                    reply = self.__game.player_input(received_json)
                                    input_span.set_attribute("player.input_processed", True)
                                    if 'player_input' in received_json:
                                        input_span.set_attribute("player.input_length", len(received_json['player_input']))
                                span.set_attribute("request.handled", "player_input")
                                
                            case comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION:
                                with create_span("end_conversation") as conv_span:
                                    reply = self.__game.end_conversation(received_json)
                                    conv_span.set_attribute("conversation.ended", True)
                                    if 'npc_name' in received_json:
                                        conv_span.set_attribute("npc.name", received_json['npc_name'])
                                span.set_attribute("request.handled", "end_conversation")
                                
                            case _:
                                reply = self.error_message(f"Request type '{request_type}' was not recognized")
                                span.set_attribute("request.handled", "unknown")
                                span.set_attribute("error.message", f"Unknown request type: {request_type}")
                    else:
                        reply = self.error_message(f"Request did not contain properly formatted json!")
                        span.set_attribute("request.has_json", False)
                        span.set_attribute("request.handled", "invalid_json")
                        span.set_attribute("error.message", "Invalid JSON in request")
                    
                    # Add response information
                    span.set_attribute("response.has_reply", bool(reply))
                    if reply:
                        span.set_attribute("response.reply_size", len(json.dumps(reply)))
                        span.set_attribute("response.reply_type", reply.get(comm_consts.KEY_REPLYTYPE, "unknown"))
                    
                    if self._show_debug_messages:
                        logging.log(self._log_level_http_out, json.dumps(reply, indent=4))
                    
                    span.set_attribute("request.status", "success")
                    return reply
                    
                except Exception as e:
                    # Record any exceptions in the span
                    span.record_exception(e)
                    span.set_attribute("request.status", "error")
                    span.set_attribute("error.type", type(e).__name__)
                    span.set_attribute("error.message", str(e))
                    
                    logging.error(f"Error processing request: {e}")
                    return self.error_message(f"Internal server error: {str(e)}")
