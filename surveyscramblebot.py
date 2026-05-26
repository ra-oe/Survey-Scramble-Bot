# 1. First-Run Auto-Installer Guard (Must run before any other imports)
import sys
import subprocess

required_libraries = {
    "websocket": "websocket-client",
    "requests": "requests"
}

for module_name, pip_name in required_libraries.items():
    try:
        __import__(module_name)
    except ImportError:
        print(f"[*] Missing required library '{pip_name}'. Installing now...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
            print(f"[+] Successfully installed '{pip_name}'!")
        except Exception as e:
            print(f"[!] Critical Error: Failed to auto-install '{pip_name}'. Please run 'pip install {pip_name}' manually.")
            print(f"Error details: {e}")
            input("\nPress Enter to exit...")
            sys.exit(1)

# 2. Standard Imports (Now completely safe to run)
import json
import uuid
import urllib.parse
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import requests
import websocket
import logging
import traceback
import re
import time
import random

class JackboxBotInstance:
    """Represents a single independent connection to the Jackbox server."""
    def __init__(self, bot_id, base_name, room_code, gui, role="player"):
        self.bot_id = bot_id
        self.bot_name = f"{base_name}_{bot_id}"
        self.room_code = room_code.upper()
        self.gui = gui
        self.role = role            # "player" or "audience"
        
        self.ws_app = None
        self.msg_sequence = 1
        self.user_index = None
        self.current_response_key = "lobbySurvey"
        self.last_handled_prompt = None
        self.last_seen_question = None  # Remembers the active round's survey question
        self.current_goal = "High"      # Track if we need High or Low popularity answers
        self.dares_base_word = None     # Remembers the original/base item in Dares mode
        self.failed_words = []          # List of words rejected by the server in this turn
        self.guessed_words = []         # List of successfully submitted words in this turn
        self.status = "Disconnected"
        self.active_prompt = "N/A"
        self.last_ai_action = "None"
        
        self.thread = None

    def start(self):
        self.status = "Connecting..."
        self.gui.update_bot_row(self)
        self.thread = threading.Thread(target=self.socket_engine, daemon=True)
        self.thread.start()

    def disconnect(self):
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        self.status = "Disconnected"
        self.active_prompt = "N/A"
        self.gui.update_bot_row(self)

    def socket_engine(self):
        host = "ecast-prod-use2.jackboxgames.com"
        user_uuid = str(uuid.uuid4())
        
        query_params = urllib.parse.urlencode({
            "role": self.role,
            "name": self.bot_name,
            "format": "json",
            "user-id": user_uuid
        })
        wss_uri = f"wss://{host}/api/v2/rooms/{self.room_code}/play?{query_params}"

        custom_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://jackbox.tv",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-WebSocket-Protocol": "ecast-v0"
        }

        def on_open(ws, *args, **kwargs):
            self.status = "TCP Connected"
            self.gui.update_bot_row(self)
            self.gui.log(f"[{self.bot_name}] Socket opened successfully. Authorizing as {self.role}...")

        def on_message(ws, message, *args, **kwargs):
            try:
                if isinstance(message, bytes):
                    message = message.decode('utf-8')
                
                frame = json.loads(message)
                opcode = frame.get("opcode")
                
                if opcode == "client/welcome":
                    profile = frame.get('result', {}).get('profile', {})
                    self.user_index = profile.get('id')
                    self.current_response_key = f"lobbySurvey:{self.user_index}"
                    self.status = f"In Lobby ({self.role.capitalize()} {self.user_index if self.user_index else ''})"
                    self.gui.update_bot_row(self)
                    self.gui.log(f"[+++] [{self.bot_name}] Successfully joined room! Assigned Index: {self.user_index}")
                
                if opcode == "object":
                    result = frame.get("result", {})
                    key = result.get("key", "")
                    val = result.get("val", {})
                    version = result.get("version", 0)
                    
                    # Track round surveys and topics dynamically to know what the current question is
                    if key == "roundInfo" and isinstance(val, dict):
                        long_p = val.get("longPrompt")
                        short_p = val.get("shortPrompt")
                        if long_p:
                            self.last_seen_question = long_p
                        elif short_p:
                            self.last_seen_question = short_p
                        
                        # Reset tracking on new round transition
                        self.current_goal = "High"
                        self.dares_base_word = None
                        self.failed_words = []
                        self.guessed_words = []

                    # Parse game text announcements to extract revealed Dare/HighLow baseline words
                    if key == "textDescriptions" and isinstance(val, dict):
                        descriptions = val.get("latestDescriptions", [])
                        for desc in descriptions:
                            desc_text = desc.get("text", "")
                            match = re.search(r"([a-zA-Z0-9\s'\-]+) is at rank \d+", desc_text, re.IGNORECASE)
                            if match:
                                self.dares_base_word = match.group(1).strip()
                                self.gui.log(f"[*] [{self.bot_name}] Dares Memory Updated! Detected active baseline word: '{self.dares_base_word}'")
                            
                    if isinstance(val, dict) and self.user_index is not None:
                        # --- STRICT OWNERSHIP GUARD ---
                        key_lower = key.lower()
                        my_index_suffix = f":{self.user_index}"
                        
                        is_target_personal = (
                            key_lower == f"player{my_index_suffix}" or
                            key_lower == f"lobbychoice{my_index_suffix}" or
                            key_lower == f"lobbysurvey{my_index_suffix}" or
                            key_lower == f"voteresponse{my_index_suffix}" or
                            key_lower == f"objectguess{my_index_suffix}" or
                            key_lower == f"textguess{my_index_suffix}"
                        )
                        
                        is_global_lobby = (key_lower == "lobby" or val.get("kind") == "lobby")

                        if not is_target_personal and not is_global_lobby and key != "roundInfo":
                            # Ignore other players' update packets to prevent state pollution
                            return

                        # Track the scoring goal (High = popular, Low = unpopular)
                        if "goal" in val:
                            self.current_goal = val.get("goal", "High")

                        # Listen for server-side word rejection feedback
                        feedback = val.get("feedback", {})
                        if feedback and feedback.get("status") == "notInList":
                            failed_word = feedback.get("word")
                            if failed_word and failed_word not in self.failed_words:
                                self.failed_words.append(failed_word)
                            
                            self.gui.log(f"[*] [{self.bot_name}] Word '{failed_word}' was NOT on the list. Resetting prompt memory and retrying...")
                            self.last_handled_prompt = None  # Clear memory to allow immediate retry
                            
                            if self.gui.ai_autoplay_var.get():
                                threading.Thread(target=self.query_ai_text, args=(self.active_prompt, self.current_response_key, version), daemon=True).start()
                            return

                        # Check for Lobby status (and let bots start or vote for game modes)
                        if val.get("kind") == "lobby":
                            response_key = f"lobbyChoice:{self.user_index}"
                            self.current_response_key = response_key
                            
                            choices = val.get("choices", [])
                            can_start = val.get("canStart", False)
                            text_entry = val.get("textEntry", {})
                            
                            if self.gui.ai_autoplay_var.get():
                                threading.Thread(
                                    target=self.handle_lobby_autoplay, 
                                    args=(choices, response_key, can_start, text_entry), 
                                    daemon=True
                                ).start()

                        # A. SQUARES (TicTacToe) Gameplay Routing Block
                        elif val.get("kind") == "ticTacToe":
                            text_key = val.get("textKey")
                            if text_key:
                                self.current_response_key = text_key
                                prompt = self.last_seen_question if self.last_seen_question else "Enter a survey guess"
                                self.active_prompt = prompt
                                self.gui.update_bot_row(self)
                                
                                if self.gui.ai_autoplay_var.get():
                                    threading.Thread(target=self.query_ai_text, args=(prompt, text_key, version), daemon=True).start()

                        # B. DARES (BetterOrWorse) Text Guess / Called Shot Phase
                        elif val.get("kind") == "dareText":
                            text_key = val.get("textResponseKey")
                            object_key = val.get("objectResponseKey")
                            successful_guess = val.get("successfulGuess")
                            show_call_shot = val.get("showCallShot", False)

                            # Handle Called Shot rank multiplier guess if unlocked
                            if show_call_shot and successful_guess and object_key:
                                self.current_response_key = object_key
                                prompt = self.last_seen_question if self.last_seen_question else "Estimate Called Shot rank"
                                self.active_prompt = f"Called Shot: {prompt}"
                                self.gui.update_bot_row(self)

                                if self.gui.ai_autoplay_var.get():
                                    threading.Thread(
                                        target=self.query_ai_called_shot, 
                                        args=(prompt, successful_guess, object_key, version), 
                                        daemon=True
                                    ).start()
                            else:
                                if text_key:
                                    self.current_response_key = text_key
                                    dare_prompt = val.get("prompt", "Enter a dare guess")
                                    
                                    prompt = self.last_seen_question if self.last_seen_question else dare_prompt
                                    self.active_prompt = prompt
                                    self.gui.update_bot_row(self)
                                    
                                    base_match = re.search(r"popular than ([a-zA-Z0-9\s'\-]+)", dare_prompt, re.IGNORECASE)
                                    if base_match:
                                        self.dares_base_word = base_match.group(1).strip()
                                    
                                    if self.gui.ai_autoplay_var.get():
                                        threading.Thread(target=self.query_ai_text, args=(prompt, text_key, version), daemon=True).start()

                        # C. DARES (BetterOrWorse) Direction Choice Phase
                        elif val.get("kind") == "dare":
                            response_key = val.get("responseKey")
                            if response_key:
                                self.current_response_key = response_key
                                instructions = val.get("instructions", "Choose Higher or Lower")
                                self.active_prompt = instructions
                                self.gui.update_bot_row(self)
                                
                                # Extract baseline from instructions if possible (e.g. "...than piracy (26)")
                                base_match = re.search(r"than ([a-zA-Z0-9\s'\-]+) \(\d+\)", instructions, re.IGNORECASE)
                                if base_match:
                                    self.dares_base_word = base_match.group(1).strip()
                                
                                if self.gui.ai_autoplay_var.get():
                                    threading.Thread(target=self.query_ai_dare_direction, args=(instructions, response_key, version), daemon=True).start()

                        # D. DASH (HorseRace) Choice / Double Down Phase
                        elif val.get("kind") == "horseRace":
                            response_key = val.get("responseKey")
                            options = val.get("options", [])
                            if response_key and options:
                                self.current_response_key = response_key
                                prompt = self.last_seen_question if self.last_seen_question else "Choose the best dash option"
                                self.active_prompt = f"Dash: {prompt}"
                                self.gui.update_bot_row(self)
                                
                                if self.gui.ai_autoplay_var.get():
                                    threading.Thread(target=self.query_ai_dash, args=(prompt, options, response_key, version), daemon=True).start()

                        # E. BOUNCE (Bricks/Pong) Gameplay Routing Block
                        elif val.get("kind") in ["bounce", "bounceText"]:
                            text_key = val.get("textKey") or val.get("textResponseKey") or val.get("responseKey")
                            if text_key:
                                self.current_response_key = text_key
                                prompt = self.last_seen_question if self.last_seen_question else "Keep ball bouncing"
                                if "instructions" in val:
                                    prompt = f"{prompt} ({val.get('instructions')})"
                                self.active_prompt = prompt
                                self.gui.update_bot_row(self)
                                
                                if self.gui.ai_autoplay_var.get():
                                    threading.Thread(target=self.query_ai_text, args=(prompt, text_key, version), daemon=True).start()

                        # F. MULTI-CHOICE / COMPARISON Matchups
                        elif "choices" in val:
                            prompt = val.get("prompt", "Compare Options" if "objectGuess" in key_lower else "Vote Option")
                            response_key = val.get("responseKey", f"objectGuess:{self.user_index}" if "objectGuess" in key_lower else f"voteResponse:{self.user_index}")
                            choices = val.get("choices", [])
                            
                            compare_match = re.search(r"Is .+? (?:MORE|LESS) popular than ([a-zA-Z0-9\s'\-]+)\??", prompt, re.IGNORECASE)
                            if compare_match:
                                self.dares_base_word = compare_match.group(1).strip()
                            
                            if response_key:
                                self.current_response_key = response_key
                                self.active_prompt = f"Voting: {prompt}"
                                self.gui.update_bot_row(self)
                                
                                if self.gui.ai_autoplay_var.get() and choices:
                                    threading.Thread(target=self.query_ai_choice, args=(prompt, choices, response_key, version), daemon=True).start()
                        
                        # G. STANDARD TEXT ENTRY Routing Block
                        elif ("textEntry" in val or "responseKey" in val) and val.get("kind") != "lobby":
                            response_key = None
                            prompt = None
                            
                            if "textEntry" in val:
                                entry_info = val["textEntry"]
                                response_key = entry_info.get("responseKey")
                                prompt = entry_info.get("prompt")
                            else:
                                response_key = val.get("responseKey")
                                prompt = self.last_seen_question if self.last_seen_question else val.get("instructions", "Guess popular word")
                            
                            if response_key:
                                # Ensure we do NOT accidentally trigger text-guesses on an active choice-voting key
                                is_choice_key = any(k in response_key.lower() for k in ["objectguess", "lobbychoice", "voteresponse"])
                                if not is_choice_key:
                                    self.current_response_key = response_key
                                    self.active_prompt = prompt
                                    self.gui.update_bot_row(self)
                                    
                                    if self.gui.ai_autoplay_var.get():
                                        threading.Thread(target=self.query_ai_text, args=(prompt, response_key, version), daemon=True).start()
                        
                        # H. SPECIAL LOBBY TEAM CHOICE PAYLOADS (for Squares)
                        elif val.get("kind") == "teamChoice":
                            response_key = val.get("responseKey", f"voteResponse:{self.user_index}")
                            # Auto lock-in on team Choice screens immediately
                            if self.gui.ai_autoplay_var.get():
                                threading.Thread(target=self.send_team_selection, args=(response_key,), daemon=True).start()

                        # I. Waiting Screens & Transient Phase transitions
                        elif val.get("kind") == "waiting" or "waiting" in val.get("kind", "").lower() or val.get("kind") in ["horseRaceWaiting", "teamWaiting", "choiceWaiting"]:
                            msg = val.get("message") or "Waiting..."
                            self.active_prompt = f"Waiting: {msg}"
                            self.last_handled_prompt = None  # Safe reset between rounds
                            self.failed_words = []
                            self.guessed_words = []
                            self.gui.update_bot_row(self)

                if opcode == "room/lock":
                    self.gui.log(f"[*] [{self.bot_name}] Game started! Room locked.")
                
            except Exception as e:
                self.gui.log(f"[!] [{self.bot_name}] Parsing Exception: {str(e)}")

        def on_error(ws, error):
            self.gui.log(f"[!] [{self.bot_name}] Network Error: {str(error)}")

        def on_close(ws, close_status_code, close_msg):
            self.status = "Disconnected"
            self.active_prompt = "N/A"
            self.gui.update_bot_row(self)
            
            close_code = close_status_code if close_status_code is not None else "None"
            close_msg_str = close_msg if close_msg is not None else "None"
            self.gui.log(f"[-] [{self.bot_name}] Disconnected. Code: {close_code}, Msg: {close_msg_str}")

        self.ws_app = websocket.WebSocketApp(
            wss_uri,
            header=custom_headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )

        try:
            self.ws_app.run_forever(ping_interval=20, ping_timeout=5, skip_utf8_validation=True)
        except Exception as e:
            self.gui.log(f"[!] [{self.bot_name}] Run error: {str(e)}")

    def send_team_selection(self, response_key):
        """Automatically distributes bots evenly across teams and locks in selection."""
        if not self.ws_app:
            return
            
        time.sleep(1.0)
        
        # Balance bots: Even bot IDs join FirstTeam, Odd bot IDs join SecondTeam
        assigned_team = "FirstTeam" if (self.bot_id % 2 == 0) else "SecondTeam"
        self.gui.log(f"[*] [{self.bot_name}] Selecting team: {assigned_team}")
        
        # 1. Dispatch setTeam payload
        payload_team = {
            "seq": self.msg_sequence,
            "opcode": "object/update",
            "params": {"key": response_key, "val": {"setTeam": assigned_team}}
        }
        self.msg_sequence += 1
        self.transmit(payload_team)
        
        # Wait briefly to simulate human transition
        time.sleep(1.5)
        
        # 2. Dispatch lockIn payload
        self.gui.log(f"[*] [{self.bot_name}] Locking in team selection.")
        payload_lock = {
            "seq": self.msg_sequence,
            "opcode": "object/update",
            "params": {"key": response_key, "val": {"lockIn": True}}
        }
        self.msg_sequence += 1
        self.transmit(payload_lock)

    def handle_lobby_autoplay(self, choices, response_key, can_start, text_entry):
        """Autonomously handles voting for game modes, answering lobby surveys, and starting."""
        state_signature = f"lobby_{self.room_code}"
        if self.last_handled_prompt == state_signature:
            # If we already cast a lobby vote, check if VIP start conditions are newly met
            if can_start and self.status == "Lobby Ready":
                self.status = "Lobby Starting"
                self.gui.update_bot_row(self)
                time.sleep(3.0)
                self.gui.log(f"[+++] [{self.bot_name}] VIP authorization active! Triggering game start payload...")
                self.send_object_response(response_key, "Start", 0)
            return

        self.last_handled_prompt = state_signature
        
        # 1. Vote for favorite Game Modes (e.g., Squares, Hilo, Speed)
        mode_index = 0
        preferred_modes = ["squares", "hilo", "speed", "tour"]
        
        for p_mode in preferred_modes:
            for idx, c in enumerate(choices):
                text = c.get("text", "").lower()
                val = c.get("value", "")
                if p_mode in text or p_mode in val.lower():
                    mode_index = idx
                    break
            else:
                continue
            break

        chosen_option = choices[mode_index]
        choice_text = chosen_option.get("text", "")
        choice_val = chosen_option.get("value", choice_text)

        self.gui.log(f"[*] [{self.bot_name}] In lobby. Voting for preferred mode: '{choice_text}'")
        self.last_ai_action = f"Voted Mode: {choice_text}"
        self.status = "Lobby Ready"
        self.gui.update_bot_row(self)
        
        # Submit the lobby mode vote
        time.sleep(1.0)
        self.send_object_response(response_key, choice_val, mode_index)

        # 2. Answer the Lobby Survey Question if present
        if isinstance(text_entry, dict) and text_entry.get("responseKey"):
            lobby_prompt = text_entry.get("prompt")
            lobby_response_key = text_entry.get("responseKey")
            
            if lobby_prompt and lobby_response_key:
                self.gui.log(f"[*] [{self.bot_name}] Answering lobby survey: '{lobby_prompt}'")
                # Immediately fire off AI question generation for lobby
                threading.Thread(target=self.query_ai_text, args=(lobby_prompt, lobby_response_key, 0), daemon=True).start()

        # 3. Handle Auto-Start if VIP/First player
        if can_start:
            time.sleep(4.0)  # Moderate wait to let other bots complete lobby surveys
            self.gui.log(f"[+++] [{self.bot_name}] VIP authorization detected! Launching lobby into game survey...")
            self.send_object_response(response_key, "Start", mode_index)

    def query_ai_text(self, prompt_text, response_key, version):
        """Asks the AI to generate a unique popular word for this specific bot."""
        # Include dynamic conditions so that each failed or successful attempt generates a fresh signature
        failed_count = len(self.failed_words)
        guessed_count = len(self.guessed_words)
        state_sig = f"text_{prompt_text}_{response_key}_{version}_{failed_count}_{guessed_count}"
        if self.last_handled_prompt == state_sig:
            return
        self.last_handled_prompt = state_sig
        
        self.status = "Thinking..."
        self.gui.update_bot_row(self)
        
        # Build dynamic goal instructions based on High/Low round rules
        if self.current_goal == "Low":
            goal_instruction = (
                "The current goal is to find an UNPOPULAR, OBSCURE, or RARE answer. "
                "Choose a word that is correct and valid but very uncommon, weird, or unlikely for most players to guess."
            )
        else:
            goal_instruction = (
                "The current goal is to find a HIGHLY POPULAR, COMMON, or TOP-OF-MIND answer. "
                "Choose a word that is extremely obvious, common, and likely to be guessed by almost everyone."
            )

        # Inject context memory if we know what the baseline compare word is in Dares/HighLow
        compare_instruction = ""
        if self.dares_base_word:
            compare_instruction = f" You are comparing against the baseline word '{self.dares_base_word}'."

        # Merge already tried, failed, and guessed words into a strict repetition guard
        avoid_words = []
        if self.failed_words:
            avoid_words.extend(self.failed_words)
        if self.guessed_words:
            avoid_words.extend(self.guessed_words)

        failed_instruction = ""
        if avoid_words:
            avoid_words = list(set(avoid_words))
            failed_instruction = f" You MUST NOT guess any of these words (they were already attempted, rejected, or guessed in this turn): {', '.join(avoid_words)}."

        system_instruction = (
            "You are playing the Jackbox survey game 'The Survey Scramble'. "
            "You must guess ONE popular, common, or funny answer that ordinary people would answer to the survey. "
            f"{goal_instruction}"
            f"{compare_instruction}"
            "You MUST respond in exactly ONE word. Do NOT explain your answer. "
            "Do NOT write 'the answer is...', 'my guess is...', or any introduction. "
            "If your answer consists of multiple words, you MUST "
            "mush them together into a single continuous word with no spaces (for example, write 'icecream' "
            "instead of 'ice cream', or 'rollercoaster' instead of 'roller coaster'). "
            "Do NOT include parentheses, alternative answers, notes, or explanations. "
            "Do NOT write any punctuation. Respond with ONLY the mushed word itself. Max 24 characters."
            f"{failed_instruction}"
        )

        try:
            ai_word = self.gui.make_ai_request(system_instruction, f"Survey Prompt: '{prompt_text}'")
            
            # --- STRICT POST-PROCESSING PIPELINE ---
            if "</think>" in ai_word:
                ai_word = ai_word.split("</think>")[-1].strip()

            # 1. Strip everything inside parentheses (e.g., "word (alternative)" -> "word")
            ai_word = re.sub(r"\(.*?\)", "", ai_word)
            ai_word = re.sub(r"\[.*?\]", "", ai_word)
            
            # 2. Clean up formatting symbols and lowercase
            ai_word = ai_word.strip().lower()
            
            # 3. Strip common conversational prefixes that smaller models use
            prefixes_to_strip = [
                "theansweris", "theanswer:", "theanswer",
                "myguessis", "myguess:", "myguess",
                "answeris", "answer:",
                "guessis", "guess:",
                "iwillchoose", "iwillguess", "ithinkis", "ithink",
                "is:", "is"
            ]
            
            # First remove spaces to make matching prefixes easier
            mushed_temp = "".join(ai_word.split())
            cleaned_prefix = False
            for p in prefixes_to_strip:
                if mushed_temp.startswith(p) and len(mushed_temp) > len(p):
                    mushed_temp = mushed_temp[len(p):]
                    cleaned_prefix = True
                    break
                    
            if cleaned_prefix:
                ai_word = mushed_temp
            
            # 4. Extract only alphabetical characters (no digits/symbols/special chars)
            ai_word = re.sub(r"[^a-zA-Z]", "", ai_word)
            
            # 5. Force mush multiple words together by stripping all inner whitespace
            ai_word = "".join(ai_word.split())
            
            # 6. Strict length cap (hard limit of 24 characters)
            ai_word = ai_word[:24]
            
            if not ai_word:
                ai_word = "unknown"

            # Cache the successful submission in our round list to prevent future repetition
            if ai_word not in self.guessed_words:
                self.guessed_words.append(ai_word)

            self.last_ai_action = f"Submitted: '{ai_word}'"
            self.status = "Active"
            self.gui.update_bot_row(self)
            self.gui.log(f"[AI] [{self.bot_name}] Decided answer: '{ai_word}' for prompt: '{prompt_text}'")
            
            time.sleep(1.5)
            self.send_text_response(response_key, ai_word)

        except Exception as e:
            self.last_ai_action = f"Failed: {str(e)}"
            self.status = "AI Error"
            self.gui.update_bot_row(self)

    def query_ai_choice(self, prompt_text, choices, response_key, version):
        """Asks the AI to choose a multiple-choice item index for this bot."""
        # Include Ecast payload "version" in the tracking state signature to allow re-evaluation on next rounds
        state_sig = f"choice_{prompt_text}_{response_key}_{version}"
        if self.last_handled_prompt == state_sig:
            return
        self.last_handled_prompt = state_sig
        
        self.status = "Thinking (Vote)..."
        self.gui.update_bot_row(self)

        options = [f"Index {idx}: {choice.get('text', '') if isinstance(choice, dict) else str(choice)}" for idx, choice in enumerate(choices)]
        options_string = "\n".join(options)

        # Inject context memory of baseline word if comparing
        compare_instruction = ""
        if self.dares_base_word:
            compare_instruction = f" Note that the baseline target we are comparing choices against is '{self.dares_base_word}'."

        system_instruction = (
            "You are playing Jackbox 'The Survey Scramble'. "
            "You must select the most popular, funny, or correct option from the provided list. "
            f"{compare_instruction}"
            "Respond with ONLY the numerical index of your chosen option. Do not type any other text."
        )

        user_content = f"Prompt Question/Comparison: '{prompt_text}'\nOptions List:\n{options_string}"

        try:
            raw_index = self.gui.make_ai_request(system_instruction, user_content)
            
            # Strip thought block if reasoner model returns it
            if "</think>" in raw_index:
                raw_index = raw_index.split("</think>")[-1].strip()

            # --- SMART MATCHING PIPELINE ---
            selected_idx = -1
            
            # Attempt 1: Check if the AI wrote out the actual choice name/text directly
            for idx, choice in enumerate(choices):
                choice_text = choice.get("text", "") if isinstance(choice, dict) else str(choice)
                if choice_text.lower() in raw_index.lower() or (isinstance(choice, dict) and choice.get("value", "").lower() in raw_index.lower()):
                    selected_idx = idx
                    break
            
            # Attempt 2: Fallback to digits extraction if no direct text matches were detected
            if selected_idx == -1:
                digits = ''.join(filter(str.isdigit, raw_index))
                selected_idx = int(digits) if digits else 0
            
            # Bounds check fallback
            if selected_idx >= len(choices) or selected_idx < 0:
                selected_idx = 0
                
            chosen_option = choices[selected_idx]
            choice_text = chosen_option.get("text", "") if isinstance(chosen_option, dict) else str(chosen_option)
            choice_val = chosen_option.get("value", choice_text) if isinstance(chosen_option, dict) else choice_text
            
            self.last_ai_action = f"Voted: '{choice_text}'"
            self.status = "Active"
            self.gui.update_bot_row(self)
            self.gui.log(f"[AI] [{self.bot_name}] Voted for index {selected_idx}: '{choice_text}'")
            
            time.sleep(1.5)
            self.send_object_response(response_key, choice_val, selected_idx)
            
        except Exception as e:
            self.last_ai_action = f"Vote Failed: {str(e)}"
            self.status = "Active"  # Reset status so bot can continue playing
            self.gui.update_bot_row(self)

    def query_ai_dare_direction(self, instructions, response_key, version):
        """Asks the LLM to choose a higher/lower direction dare targeting the next player."""
        state_sig = f"dare_dir_{instructions}_{response_key}_{version}"
        if self.last_handled_prompt == state_sig:
            return
        self.last_handled_prompt = state_sig
        
        self.status = "Thinking (Dare)..."
        self.gui.update_bot_row(self)

        # Inject context memory if we know what word we are starting from
        compare_instruction = ""
        if self.dares_base_word:
            compare_instruction = f" The current reference word is '{self.dares_base_word}'."
        
        system_instruction = (
            "You are playing the Jackbox survey game 'The Survey Scramble' Dares mode. "
            "You must decide if the next player should guess an item MORE popular ('Higher') or LESS popular ('Lower') than the current word. "
            f"{compare_instruction}"
            "Think strategically: is it easier to find an answer more popular or less popular than the reference word? "
            "Respond with exactly one word: 'Higher' or 'Lower'."
        )
        
        try:
            raw_res = self.gui.make_ai_request(system_instruction, instructions)
            if "</think>" in raw_res:
                raw_res = raw_res.split("</think>")[-1].strip()
                
            direction = "Lower" if "low" in raw_res.lower() else "Higher"
            
            self.last_ai_action = f"Dared: {direction}"
            self.status = "Active"
            self.gui.update_bot_row(self)
            self.gui.log(f"[AI] [{self.bot_name}] Dared next player to go {direction}")
            
            time.sleep(1.5)
            payload = {
                "seq": self.msg_sequence,
                "opcode": "object/update",
                "params": {"key": response_key, "val": {"points": None, "direction": direction}}
            }
            self.msg_sequence += 1
            self.transmit(payload)
            
        except Exception as e:
            self.last_ai_action = f"Dare direction failed: {str(e)}"
            self.status = "Active"
            self.gui.update_bot_row(self)

    def query_ai_dash(self, prompt, options, response_key, version):
        """Asks the LLM to choose the best option from the list during Dash matches."""
        # Include Ecast state layout "version" in signature to allow multiple matchup votes to process!
        state_sig = f"dash_{prompt}_{response_key}_{version}"
        if self.last_handled_prompt == state_sig:
            return
        self.last_handled_prompt = state_sig
        
        self.status = "Thinking (Dash)..."
        self.gui.update_bot_row(self)
        
        options_str = "\n".join([f"Index {idx}: {opt}" for idx, opt in enumerate(options)])
        system_instruction = (
            "You are playing the Jackbox survey game 'The Survey Scramble' Dash (Horse Race) mode. "
            "Choose the option from the list that best answers the prompt. "
            "Respond with ONLY the numerical index of your choice. Do not write anything else."
        )
        user_content = f"Survey Prompt: '{prompt}'\nOptions:\n{options_str}"
        
        try:
            raw_idx = self.gui.make_ai_request(system_instruction, user_content)
            if "</think>" in raw_idx:
                raw_idx = raw_idx.split("</think>")[-1].strip()
                
            digits = ''.join(filter(str.isdigit, raw_idx))
            selected_idx = int(digits) if digits else 0
            
            if selected_idx >= len(options) or selected_idx < 0:
                selected_idx = 0
            
            # Simulated human behavior: 35% chance to Double Down
            double_down = random.random() < 0.35
            
            option_text = options[selected_idx]
            self.last_ai_action = f"Dash: {option_text} (Double Down: {double_down})"
            self.status = "Active"
            self.gui.update_bot_row(self)
            self.gui.log(f"[AI] [{self.bot_name}] Dash choice: {option_text} (Double Down: {double_down})")
            
            time.sleep(1.5)
            payload = {
                "seq": self.msg_sequence,
                "opcode": "object/update",
                "params": {"key": response_key, "val": {"optionIndex": selected_idx, "doubledDown": double_down}}
            }
            self.msg_sequence += 1
            self.transmit(payload)
            
        except Exception as e:
            self.last_ai_action = f"Dash failed: {str(e)}"
            self.status = "Active"
            self.gui.update_bot_row(self)

    def query_ai_called_shot(self, prompt, successful_guess, response_key, version):
        """Asks the LLM to choose a called shot rank multiplier guess during Dares matches."""
        state_sig = f"called_shot_{successful_guess}_{response_key}_{version}"
        if self.last_handled_prompt == state_sig:
            return
        self.last_handled_prompt = state_sig
        
        self.status = "Thinking (Called Shot)..."
        self.gui.update_bot_row(self)
        
        system_instruction = (
            "You are playing the Jackbox survey game 'The Survey Scramble' Dares mode. "
            "You made a successful guess. Now you must guess the exact popular ranking of your word in the survey. "
            "Respond with ONLY a single estimated rank integer between 1 and 100 (where 1 is the most popular/top answer). "
            "Output ONLY the raw integer number with no extra text."
        )
        user_content = f"Active Survey Prompt: '{prompt}'\nYour successful guess: '{successful_guess}'"
        
        try:
            raw_rank = self.gui.make_ai_request(system_instruction, user_content)
            if "</think>" in raw_rank:
                raw_rank = raw_rank.split("</think>")[-1].strip()
                
            digits = ''.join(filter(str.isdigit, raw_rank))
            rank_val = int(digits) if digits else random.randint(1, 15)
            
            # Constrain to reasonable bounds
            if rank_val < 1 or rank_val > 500:
                rank_val = random.randint(1, 15)
                
            self.last_ai_action = f"Called Shot: Rank {rank_val}"
            self.status = "Active"
            self.gui.update_bot_row(self)
            self.gui.log(f"[AI] [{self.bot_name}] Called Shot Rank guess for '{successful_guess}': {rank_val}")
            
            time.sleep(1.5)
            payload = {
                "seq": self.msg_sequence,
                "opcode": "object/update",
                "params": {"key": response_key, "val": {"calledShotRank": rank_val}}
            }
            self.msg_sequence += 1
            self.transmit(payload)
            
        except Exception as e:
            self.last_ai_action = f"Called shot failed: {str(e)}"
            self.status = "Active"
            self.gui.update_bot_row(self)

    def send_text_response(self, target_key, value):
        if not self.ws_app:
            return
        payload = {
            "seq": self.msg_sequence,
            "opcode": "text/update",
            "params": {"key": target_key, "val": value}
        }
        self.msg_sequence += 1
        self.transmit(payload)

    def send_object_response(self, target_key, value, index):
        if not self.ws_app:
            return
        
        # Dynamic response payload structure based on target key type
        target_key_lower = target_key.lower()
        if "lobbychoice" in target_key_lower:
            val_obj = {"voteMode": value}
        elif "voteresponse" in target_key_lower or "objectguess" in target_key_lower:
            # Perfect Ecast alignment for matchups and topic votes
            val_obj = {"action": "choice", "value": index}
        else:
            val_obj = {"index": index}

        payload = {
            "seq": self.msg_sequence,
            "opcode": "object/update",
            "params": {"key": target_key, "val": val_obj}
        }
        self.msg_sequence += 1
        self.transmit(payload)

    def transmit(self, payload):
        try:
            self.ws_app.send(json.dumps(payload))
        except Exception as e:
            self.gui.log(f"[!] [{self.bot_name}] Send failed: {str(e)}")


class JackboxMultiBotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Jackbox Survey Scramble bot")
        self.root.geometry("1100x870")
        
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        self.bots = {}
        self.bot_counter = 1
        
        self.create_widgets()
        self.setup_raw_logging()

    def create_widgets(self):
        # 1. Connection & AI Config Frame
        global_frame = ttk.LabelFrame(self.root, text=" Global Lobby & AI Config ", padding=10)
        global_frame.pack(fill="x", padx=10, pady=5)
        
        # Room Code inputs
        ttk.Label(global_frame, text="Room Code:").grid(row=0, column=0, sticky="w", pady=2)
        self.room_entry = ttk.Entry(global_frame, width=10, font=("Arial", 11, "bold"))
        self.room_entry.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        
        ttk.Label(global_frame, text="Base Bot Name:").grid(row=0, column=2, sticky="w", pady=2, padx=15)
        self.base_name_entry = ttk.Entry(global_frame, width=15, font=("Arial", 10))
        self.base_name_entry.insert(0, "AIBot")
        self.base_name_entry.grid(row=0, column=3, sticky="w", padx=5, pady=2)
        
        # Spawn Buttons
        spawn_btn_frame = ttk.Frame(global_frame)
        spawn_btn_frame.grid(row=0, column=4, padx=20)
        
        self.spawn_btn = ttk.Button(spawn_btn_frame, text="Spawn Bot", command=self.pre_spawn_room_check)
        self.spawn_btn.pack(side="left", padx=5)
        
        self.disconnect_all_btn = ttk.Button(spawn_btn_frame, text="Disconnect All", command=self.disconnect_all_bots)
        self.disconnect_all_btn.pack(side="left", padx=5)

        # AI Configuration Row
        ttk.Label(global_frame, text="AI Provider:").grid(row=1, column=0, sticky="w", pady=8)
        self.provider_var = tk.StringVar(value="Ollama")
        self.provider_dropdown = ttk.Combobox(global_frame, textvariable=self.provider_var, state="readonly", width=18)
        self.provider_dropdown['values'] = ("Ollama", "DeepSeek", "OpenRouter")
        self.provider_dropdown.grid(row=1, column=1, sticky="w", padx=5, pady=8)
        self.provider_dropdown.bind("<<ComboboxSelected>>", self.on_provider_change)

        ttk.Label(global_frame, text="Model:").grid(row=1, column=2, sticky="w", pady=8, padx=15)
        self.model_var = tk.StringVar(value="Click the button to the right")
        self.model_dropdown = ttk.Combobox(global_frame, textvariable=self.model_var, state="readonly", width=30)
        self.model_dropdown['values'] = (
            "Click the button to the right"
        )
        self.model_dropdown.grid(row=1, column=2, columnspan=2, sticky="w", padx=5, pady=8)

        # Refresh button next to model selection dropdown
        self.refresh_models_btn = ttk.Button(global_frame, text="Refresh Ollama AIs", width=45, command=self.manual_refresh_models)
        self.refresh_models_btn.grid(row=1, column=4, sticky="w", padx=5, pady=8)

        ttk.Label(global_frame, text="API Key / Local URL:").grid(row=2, column=0, sticky="w", pady=2)
        self.api_key_entry = ttk.Entry(global_frame, width=35,)
        self.api_key_entry.grid(row=2, column=1, columnspan=2, sticky="w", padx=5, pady=2)
        self.api_key_entry.insert(0, "http://localhost:11434")
        
        self.ai_autoplay_var = tk.BooleanVar(value=True)
        self.ai_toggle = ttk.Checkbutton(global_frame, text="Enable AI (All Bots)", variable=self.ai_autoplay_var)
        self.ai_toggle.grid(row=2, column=3, columnspan=2, sticky="w", padx=15, pady=2)

        # Game Compatibility Notice Label
        notice_frame = ttk.Frame(global_frame)
        notice_frame.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(5, 0))
        self.notice_lbl = ttk.Label(
            notice_frame, 
            text="Note: This bot ONLY works for The Survey Scramble game. Connecting to other rooms is supported, but the bot cannot play.",
            foreground="#A04000",
            font=("Arial", 9, "bold")
        )
        self.notice_lbl.pack(side="left", padx=5)

        # 2. Grid/Table of Connected Bots
        table_frame = ttk.LabelFrame(self.root, text=" Active Bots Manager ", padding=10)
        table_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        columns = ("id", "name", "index", "status", "prompt", "last_action")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        
        self.tree.heading("id", text="Bot ID")
        self.tree.heading("name", text="Bot Name")
        self.tree.heading("index", text="Player Index")
        self.tree.heading("status", text="Status")
        self.tree.heading("prompt", text="Current Prompt")
        self.tree.heading("last_action", text="Last AI Action")
        
        self.tree.column("id", width=60, anchor="center")
        self.tree.column("name", width=120, anchor="w")
        self.tree.column("index", width=100, anchor="center")
        self.tree.column("status", width=140, anchor="center")
        self.tree.column("prompt", width=250, anchor="w")
        self.tree.column("last_action", width=250, anchor="w")
        
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        self.tree.bind("<<TreeviewSelect>>", self.on_bot_selected)

        # 3. Manual Override Controls for Selected Bot
        self.override_frame = ttk.LabelFrame(self.root, text=" Manual Override Panel (Selected Bot) ", padding=10)
        self.override_frame.pack(fill="x", padx=10, pady=5)
        
        self.override_lbl = ttk.Label(self.override_frame, text="No bot selected. Select a row above to send manual inputs.", foreground="gray", font=("Arial", 9, "italic"))
        self.override_lbl.grid(row=0, column=0, columnspan=3, sticky="w", pady=5)
        
        self.response_entry = ttk.Entry(self.override_frame, width=40, font=("Arial", 10))
        self.response_entry.grid(row=1, column=0, sticky="w", padx=5)
        self.response_entry.config(state="disabled")
        self.response_entry.bind("<Return>", lambda e: self.send_manual_text())
        
        self.send_text_btn = ttk.Button(self.override_frame, text="Submit Manual Word", command=self.send_manual_text)
        self.send_text_btn.grid(row=1, column=1, padx=5)
        self.send_text_btn.config(state="disabled")

        self.disconnect_one_btn = ttk.Button(self.override_frame, text="Disconnect Bot", command=self.disconnect_selected_bot)
        self.disconnect_one_btn.grid(row=1, column=2, padx=15)
        self.disconnect_one_btn.config(state="disabled")

        # 4. Global Logging console
        log_frame = ttk.LabelFrame(self.root, text=" Shared Event Console Monitor & Raw Wire Trace ", padding=10)
        log_frame.pack(fill="both", expand=False, padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, background="black", foreground="#00FF00", font=("Consolas", 9), height=10)
        self.log_area.pack(fill="both", expand=True)
        self.log("[SYSTEM] Ready. Set Room Code and click Spawn Bot.")

        # Log control button panel
        log_ctrl_frame = ttk.Frame(log_frame)
        log_ctrl_frame.pack(fill="x", pady=5)
        
        self.export_btn = ttk.Button(log_ctrl_frame, text="Export Console Log", command=self.export_log)
        self.export_btn.pack(side="left", padx=5)
        
        self.clear_btn = ttk.Button(log_ctrl_frame, text="Clear Console", command=self.clear_log)
        self.clear_btn.pack(side="left", padx=5)

    def log(self, text):
        self.root.after(0, self._safe_log, text)

    def _safe_log(self, text):
        self.log_area.insert(tk.END, f"{text}\n")
        self.log_area.see(tk.END)

    def export_log(self):
        """Saves all logged trace events to a text file."""
        try:
            log_content = self.log_area.get("1.0", tk.END).strip()
            if not log_content:
                self.log("[!] System: Console is empty, nothing to export.")
                return
            
            file_path = filedialog.asksaveasfilename(
                initialfile="jackbox_bot_console_log.txt",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                title="Export Console Log"
            )
            
            if file_path:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(log_content)
                self.log(f"[+] System: Console log successfully exported to: {file_path}")
        except Exception as e:
            self.log(f"[!] Export Failed: {str(e)}")

    def clear_log(self):
        """Clears the console log window."""
        self.log_area.delete("1.0", tk.END)
        self.log("[SYSTEM] Console cleared.")

    def setup_raw_logging(self):
        """Redirects websocket-client trace diagnostics directly to the GUI console."""
        websocket.enableTrace(True)
        
        logger = logging.getLogger('websocket')
        logger.setLevel(logging.DEBUG)
        
        class TkinterLogHandler(logging.Handler):
            def __init__(self, gui):
                super().__init__()
                self.gui = gui
            def emit(self, record):
                self.gui.log(f"[WIRE] {self.format(record)}")
                
        handler = TkinterLogHandler(self)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)

    def on_provider_change(self, event):
        provider = self.provider_var.get()
        if provider == "OpenRouter":
            self.model_dropdown['values'] = (
                "meta-llama/llama-3-8b-instruct:free",
                "google/gemma-2-9b-it:free",
                "mistralai/mistral-7b-instruct:free",
                "deepseek/deepseek-chat:free"
            )
            self.model_var.set("meta-llama/llama-3-8b-instruct:free")
            self.api_key_entry.config(state="normal", show="*")
            self.api_key_entry.delete(0, tk.END)
        elif provider == "DeepSeek":
            self.model_dropdown['values'] = ("deepseek-chat", "deepseek-reasoner")
            self.model_var.set("deepseek-chat")
            self.api_key_entry.config(state="normal", show="*")
            self.api_key_entry.delete(0, tk.END)
        elif provider == "Ollama":
            self.model_dropdown['values'] = ("Click the button to the right", "")
            self.api_key_entry.config(state="normal", show="")
            self.api_key_entry.delete(0, tk.END)
            self.api_key_entry.insert(0, "http://localhost:11434")
            self.fetch_ollama_models()

    def fetch_ollama_models(self):
        """Asks local Ollama instance for the currently installed/downloaded models."""
        base_url = self.api_key_entry.get().strip()
        if not base_url:
            base_url = "http://localhost:11434"
        
        base_url = base_url.rstrip("/")

        def worker():
            try:
                self.log("[*] Querying local Ollama service for installed models...")
                url = f"{base_url}/api/tags"
                response = requests.get(url, timeout=3)
                if response.status_code == 200:
                    data = response.json()
                    models = [m["name"] for m in data.get("models", [])]
                    if models:
                        self.root.after(0, lambda: self.update_ollama_model_list(models))
                        self.log(f"[+] Successfully loaded local models: {', '.join(models)}")
                    else:
                        self.log("[!] Ollama is running, but no models are installed locally. Use 'ollama run <model>' in your terminal first.")
                        self.root.after(0, lambda: self.update_ollama_model_list(["llama3", "gemma2", "mistral", "phi3"]))
                else:
                    raise Exception(f"HTTP Status {response.status_code}")
            except Exception as e:
                self.log(f"[!] Could not connect to local Ollama service ({base_url}): {str(e)}. Falling back to standard default presets.")
                self.root.after(0, lambda: self.update_ollama_model_list(["llama3", "gemma2", "mistral", "phi3"]))

        threading.Thread(target=worker, daemon=True).start()

    def update_ollama_model_list(self, models):
        self.model_dropdown['values'] = tuple(models)
        if models:
            self.model_var.set(models[0])

    def manual_refresh_models(self):
        """Action handler for the refresh button."""
        provider = self.provider_var.get()
        if provider == "Ollama":
            self.fetch_ollama_models()
        else:
            self.log("[*] Model refresh is only applicable when 'Ollama' is chosen as your active provider.")

    def pre_spawn_room_check(self):
        """Interviews the ecast HTTP API to ensure the room code is valid, targets Survey Scramble, and checks player capacity."""
        room = self.room_entry.get().strip().upper()
        if not room or len(room) != 4:
            self.log("[!] System Error: Cannot spawn. Room code must be 4 characters.")
            return

        def api_check_worker():
            try:
                self.log(f"[*] Querying Jackbox Ecast Room API for metadata of room '{room}'...")
                # Restored to the most reliable public ecast gateway endpoint
                url = f"https://ecast.jackboxgames.com/api/v2/rooms/{room}"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json"
                }
                response = requests.get(url, headers=headers, timeout=5)
                
                # Check for "Room Code doesn't exist" / "Room not found"
                if response.status_code == 404:
                    self.log(f"[!] Error: Room '{room}' does not exist.")
                    self.root.after(0, lambda: messagebox.showerror("Room Not Found", f"Room '{room}' is not an active Jackbox lobby. Please check the code and try again."))
                    return
                
                # If API response fails with other codes, gracefully fallback to spawning player directly to bypass connection blockers
                if response.status_code != 200:
                    self.log(f"[!] API returned status {response.status_code}. Bypassing pre-check and attempting WebSocket connection...")
                    self.root.after(0, lambda: self.proceed_to_spawn(room, "player"))
                    return

                try:
                    room_data = response.json()
                except Exception:
                    self.log("[!] Failed to parse JSON. Bypassing pre-check and attempting connection...")
                    self.root.after(0, lambda: self.proceed_to_spawn(room, "player"))
                    return

                # Ensure "ok" status is valid
                if room_data.get("ok") is False or "error" in room_data:
                    self.log(f"[!] Error: Room '{room}' not found or inactive.")
                    self.root.after(0, lambda: messagebox.showerror("Room Not Found", f"Room '{room}' is not an active Jackbox lobby. Please check the code and try again."))
                    return

                # Safe Extraction of Nested Body Object containing actual metadata
                body_data = room_data.get("body", {})
                if not isinstance(body_data, dict):
                    body_data = {}

                # Check Twitch Lock (Requirement)
                twitch_locked = body_data.get("twitchLocked", False)
                if twitch_locked:
                    self.log(f"[!] Error: Room '{room}' requires Twitch login authorization.")
                    self.root.after(0, lambda: messagebox.showerror(
                        "Twitch Required", 
                        "The room requires you to be logged into Twitch. The bot cannot join."
                    ))
                    return

                # Verify game compatibility strictly against lowercase "bigsurvey" (Requirement)
                app_tag = body_data.get("appTag", body_data.get("apptag", ""))
                is_survey_scramble = (app_tag == "bigsurvey")
                
                # Check if locked (game started)
                is_locked = body_data.get("locked", False)

                # Check player limits to detect full rooms
                num_players = body_data.get("numPlayers", 0)
                max_players = body_data.get("maxPlayers", 8)
                is_full = body_data.get("full", False) or (num_players >= max_players) or body_data.get("audienceRequired", False)
                
                if not is_survey_scramble:
                    self.log(f"[!] Warning: Room hosting a different game mode ({app_tag}).")
                    self.root.after(0, lambda: messagebox.showwarning(
                        "Different Game Detected", 
                        f"This room is hosting a different Jackbox game ({app_tag})!\n\n"
                        "Note: This bot currently ONLY works for 'The Survey Scramble'. "
                        "The bot may join, but it will do nothing!"
                    ))
                
                if is_locked:
                    self.log(f"[*] Room is locked (game has already started). Promoting audience connection...")
                    self.root.after(0, lambda: self.prompt_locked_audience_modal(room))
                elif is_full:
                    self.log(f"[*] Room is full ({num_players}/{max_players} players). Promoting audience connection...")
                    self.root.after(0, lambda: self.prompt_audience_modal(room))
                else:
                    self.root.after(0, lambda: self.proceed_to_spawn(room, "player"))
                    
            except Exception as e:
                # Handle unexpected API failure safely by attempting the direct connection anyway
                self.log(f"[!] Pre-join check encountered an issue: {str(e)}. Proceeding with fallback connection...")
                self.root.after(0, lambda: self.proceed_to_spawn(room, "player"))

        threading.Thread(target=api_check_worker, daemon=True).start()

    def prompt_locked_audience_modal(self, room_code):
        """Asks the user if they'd like to join as an Audience observer since the game has already started."""
        ans = messagebox.askyesno(
            "Game Started",
            "Game has started. Join as audience?"
        )
        if ans:
            self.proceed_to_spawn(room_code, "audience")
        else:
            self.log("[-] Connection canceled because the game has already started.")

    def prompt_audience_modal(self, room_code):
        """Asks the user if they'd like to join as an Audience observer since the player slots are full."""
        ans = messagebox.askyesno(
            "Room Full",
            f"The room '{room_code}' is currently full.\n\n"
            "Would you like to join as Audience instead?"
        )
        if ans:
            self.proceed_to_spawn(room_code, "audience")
        else:
            self.log("[-] Connection canceled due to lack of available player slots.")

    def proceed_to_spawn(self, room, role):
        """Launches the instance worker thread."""
        bot_id = self.bot_counter
        self.bot_counter += 1
        base_name = self.base_name_entry.get().strip()
        
        bot = JackboxBotInstance(bot_id, base_name, room, self, role=role)
        self.bots[bot_id] = bot
        
        self.tree.insert("", "end", iid=str(bot_id), values=(bot_id, bot.bot_name, "N/A", "Connecting...", "N/A", "None"))
        bot.start()

    def update_bot_row(self, bot):
        self.root.after(0, self._safe_update_row, bot)

    def _safe_update_row(self, bot):
        iid = str(bot.bot_id)
        if self.tree.exists(iid):
            idx_str = str(bot.user_index) if bot.user_index is not None else "N/A"
            role_label = f" ({bot.role.capitalize()})" if bot.role != "player" else ""
            self.tree.item(iid, values=(bot.bot_id, bot.bot_name, f"{idx_str}{role_label}", bot.status, bot.active_prompt, bot.last_ai_action))

    def on_bot_selected(self, event):
        selected_item = self.tree.selection()
        if not selected_item:
            return
        
        bot_id = int(selected_item[0])
        bot = self.bots.get(bot_id)
        if bot:
            self.override_lbl.config(text=f"Direct Override Target: {bot.bot_name}", foreground="blue", font=("Arial", 9, "bold"))
            self.response_entry.config(state="normal")
            self.send_text_btn.config(state="normal")
            self.disconnect_one_btn.config(state="normal")

    def disconnect_selected_bot(self):
        selected_item = self.tree.selection()
        if not selected_item:
            return
        bot_id = int(selected_item[0])
        bot = self.bots.get(bot_id)
        if bot:
            bot.disconnect()
            self.log(f"[*] Manual Command: Disconnected {bot.bot_name}.")

    def disconnect_all_bots(self):
        for bot in self.bots.values():
            bot.disconnect()
        self.log("[*] Manual Command: Disconnected all bots.")

    def send_manual_text(self):
        selected_item = self.tree.selection()
        if not selected_item:
            return
        bot_id = int(selected_item[0])
        bot = self.bots.get(bot_id)
        val = self.response_entry.get().strip()
        if bot and val:
            bot.send_text_response(bot.current_response_key, val)
            self.log(f"[MANUAL OVERRIDE] [{bot.bot_name}] Sent: '{val}' to key {bot.current_response_key}")
            self.response_entry.delete(0, tk.END)

    def make_ai_request(self, system_prompt, user_prompt):
        api_key = self.api_key_entry.get().strip()
        provider = self.provider_var.get()
        model = self.model_var.get()

        # Handle local Ollama requests cleanly
        if provider == "Ollama":
            base_url = api_key if api_key else "http://localhost:11434"
            base_url = base_url.rstrip("/")
            url = f"{base_url}/api/chat"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "stream": False
            }
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            res_json = response.json()
            raw_output = res_json['message']['content'].strip()
            return raw_output

        # Cloud API key sanitization
        if not api_key:
            raise ValueError("No API Key entered!")

        if api_key.lower().startswith("bearer "):
            api_key = api_key[7:].strip()
        else:
            api_key = api_key.strip()

        if provider == "OpenRouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
        else:
            url = "https://api.deepseek.com/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.8
        }

        if "reasoner" in model or "r1" in model:
            payload["messages"] = [
                {"role": "user", "content": f"{system_prompt}\n\nTask:\n{user_prompt}"}
            ]

        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
        response.raise_for_status()
        
        res_json = response.json()
        raw_output = res_json['choices'][0]['message']['content'].strip()
        
        if "</think>" in raw_output:
            raw_output = raw_output.split("</think>")[-1].strip()
            
        return raw_output


# 3. Safe Window Execution Engine (Guards against unexpected instant crashes)
if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = JackboxMultiBotGUI(root)
        root.mainloop()
    except Exception as e:
        error_msg = traceback.format_exc()
        with open("crash_log.txt", "w") as f:
            f.write(error_msg)
        
        print("\n" + "!" * 60)
        print("CRITICAL LAUNCH ERROR DETECTED!")
        print("Details have been successfully dumped to 'crash_log.txt'")
        print("!" * 60 + "\n")
        print(error_msg)
        input("\nPress ENTER to close this window...")