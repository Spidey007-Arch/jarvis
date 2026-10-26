"""Native speech-to-speech with Gemini Live.

The agent streams the microphone here (16 kHz PCM); this relays it to a Gemini Live session and sends
the spoken reply (24 kHz PCM) back to the agent's speakers as it arrives. Gemini hears the user
directly, answers in its own voice and can call JARVIS's tools. JARVIS is not cut off by whatever the
microphone picks up while it talks (Google's NO_INTERRUPTION mode, and the agent sends silence meanwhile);
saying "Hey JARVIS" over it stops it (barge()). Both sides are transcribed, and every finished exchange is
saved as a normal conversation. A session opened within CONTINUE_S of the last exchange continues that
conversation, so a pause or an interruption does not start a new one.

A session opens on the wake word or push-to-talk and closes after IDLE_S of quiet, or when the user
says something like "that's all". With an opening message (the start-up briefing) JARVIS speaks first.
It talks to Google directly with the saved Gemini key. If Google drops the connection mid-conversation, it reconnects and resumes
the same conversation with a session-resumption handle, and asks JARVIS to finish a cut-off reply.
If the line is too slow for live audio (a reply arrives slower than it plays, or the connection drops twice),
the session ends and on_slow is called, so JARVIS can carry on with the offline voice instead."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger("jarvis.live")

MODELS = ("gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest")
VOICE = "Charon"  # a deep, composed male voice
IDLE_S = 15.0
RECONNECTS = 3  # a dropped connection is reopened (and the conversation resumed) this many times
CONTINUE_S = 10 * 60  # a new session within this long of the last exchange carries on that conversation
SLOW_CHECK_S = 3.0  # how long into a reply before judging whether its audio keeps up
SLOW_RATIO = 0.8  # audio arriving slower than this fraction of real time breaks up while it plays
BYTES_PER_S = 48000  # Gemini's 24 kHz 16-bit mono
STALL_S = 6.0  # mid-reply with nothing from Google this long: treat the connection as dead and reconnect
_BYE = re.compile(r"\b(that'?s all|that will be all|that'?ll be all|goodbye|good night|bye jarvis|stop listening|thank you,? that'?s it)\b", re.I)


class LiveError(Exception):
    pass


class SlowLine(LiveError):
    """The connection cannot carry live audio in real time."""


class LiveVoice:
    def __init__(
        self,
        api_key: Callable[[], str],
        push_agent: Callable[[dict[str, Any]], Awaitable[None]],
        call_agent: Callable[..., Awaitable[Any]],
        emit: Callable[[dict[str, Any]], None],
        system_prompt: Callable[[], str],
        tools: list[dict[str, Any]],
        run_tool: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        save_turn: Callable[[str | None, str, str, str | None], str],
        after_turn: Callable[[str, str, str], None] | None = None,
        recent_conversation: Callable[[float], str | None] = lambda _age: None,
        on_slow: Callable[[str, str, bool, str], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.push_agent = push_agent
        self.call_agent = call_agent
        self.emit = emit
        self.system_prompt = system_prompt
        self.tools = tools
        self.run_tool = run_tool
        self.heard = ""  # what the user said in the current turn, for spoken confirmations
        self.turns = 0
        self.save_turn = save_turn
        self.after_turn = after_turn
        self.recent_conversation = recent_conversation
        self.on_slow = on_slow  # (reason, what the user just said, whether a tool already ran for it, origin)
        self.conversation_id: str | None = None  # the conversation this session adds to (its history goes in the prompt)
        self._barged = False  # the user said "Hey JARVIS" over the reply: drop the rest of it
        self._task: asyncio.Task | None = None
        self._mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)  # ~4 s; older audio is dropped on a slow line
        self._stop = asyncio.Event()
        self._available: list[str] | None = None
        self._handle: str | None = None  # Google's handle for resuming this conversation after a drop

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    # ----- control -----

    async def start(self, origin: str, opening: str | None = None, label: str | None = None, wait: bool = False,
                    said: str | None = None) -> dict[str, Any]:
        """Open a session. opening: a text turn JARVIS answers out loud before listening; said: what the user
        already said to wake JARVIS (answered first, and saved as their words); label titles the saved
        conversation. wait: return only once connected, raising LiveError if it cannot connect."""
        if self.active:
            return {"live": True, "already": True}
        if not self.api_key():
            raise LiveError("Live voice needs a Google Gemini key saved in Settings.")
        self._stop = asyncio.Event()
        self._mic = asyncio.Queue(maxsize=100)
        opened: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(origin, opening or said, label, opened, said or ""))
        if wait:
            err = await opened
            if err:
                raise LiveError(err)
        return {"live": True}

    async def stop(self) -> None:
        self._stop.set()

    async def barge(self) -> None:
        """The user said "Hey JARVIS" while JARVIS was talking: stop the reply now and listen."""
        if not self.active:
            return
        self._barged = True
        await self.push_agent({"type": "speaker_flush"})
        log.info("live: reply stopped by the wake word")

    def feed(self, b64: str) -> None:
        """A 40 ms microphone frame from the agent."""
        if not self.active:
            return
        try:
            self._mic.put_nowait(base64.b64decode(b64))
        except asyncio.QueueFull:
            self._mic.get_nowait()  # the line is too slow: drop the oldest audio, keep the conversation current
            self._mic.put_nowait(base64.b64decode(b64))

    # ----- session -----

    def _models(self, client: Any) -> list[str]:
        if self._available is None:
            try:
                names = {m.name.rsplit("/", 1)[-1] for m in client.models.list()}
                self._available = [m for m in MODELS if m in names] or list(MODELS)
            except Exception:  # noqa: BLE001 - listing failed; just try them in order
                self._available = list(MODELS)
        return self._available

    def _config(self, types: Any, voice: bool, resume: bool = False, steady: bool = True) -> Any:
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in self.tools]
        kw: dict[str, Any] = dict(
            response_modalities=["AUDIO"],
            system_instruction=self.system_prompt(),
            tools=[types.Tool(function_declarations=decls)],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        if steady:  # the user's audio does not cut JARVIS off mid-sentence
            kw["realtime_input_config"] = types.RealtimeInputConfig(activity_handling=types.ActivityHandling.NO_INTERRUPTION)
        if resume:
            kw["session_resumption"] = types.SessionResumptionConfig(handle=self._handle)
        if voice:
            kw["speech_config"] = types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)))
        return types.LiveConnectConfig(**kw)

    async def _connect(self, client: Any, types: Any, models: list[str] | None = None) -> tuple[Any, Any, str]:
        last: Exception | None = None
        for model in models or self._models(client):
            # some models refuse a voice choice, session resumption or the no-interruption mode; then go without
            for voice, resume, steady in ((True, True, True), (True, False, True), (True, False, False), (False, False, False)):
                try:
                    cm = client.aio.live.connect(model=model, config=self._config(types, voice, resume, steady))
                    session = await asyncio.wait_for(cm.__aenter__(), 20)
                    return cm, session, model
                except Exception as e:  # noqa: BLE001
                    last = e
                    log.warning("live connect failed",
                                extra={"fields": {"model": model, "voice": voice, "resume": resume, "steady": steady, "error": str(e)[:200]}})
        raise LiveError(f"Could not open a live voice session: {last}")

    async def _send_text(self, session: Any, types: Any, text: str) -> None:
        try:
            await session.send_realtime_input(text=text)  # Gemini 3 live models take text as realtime input
        except Exception:  # noqa: BLE001 - older live models take it as a client turn
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)

    async def _run(self, origin: str, opening: str | None, label: str | None, opened: asyncio.Future, said: str = "") -> None:
        from google import genai
        from google.genai import types

        # carry on the conversation from a few minutes ago; the start-up briefing always starts its own
        conv_id: str | None = None if label else self.recent_conversation(CONTINUE_S)
        self.conversation_id = conv_id
        self._handle = None
        self._barged = False
        try:
            client = genai.Client(api_key=self.api_key())
            self.emit({"event": "live_connecting", "origin": origin})
            cm, session, model = await self._connect(client, types)
        except Exception as e:  # noqa: BLE001
            self.emit({"event": "error", "message": str(e)})
            opened.set_result(str(e))
            return
        opened.set_result(None)
        log.info("live session open", extra={"fields": {"model": model, "origin": origin}})
        state = {"last": time.monotonic(), "playing_until": 0.0, "user": said, "jarvis": "", "bye": False, "opening": bool(opening), "done": False, "reconnecting": False, "heard_at": time.monotonic(),
                 "reply_t0": 0.0, "reply_bytes": 0, "acted": False}
        reconnects = 0
        slow: SlowLine | None = None
        try:
            await self.call_agent("live_mic", {"on": True}, timeout=10)
            self.emit({"event": "live_start", "model": model, "origin": origin})
            if said:
                self.emit({"event": "user_text", "text": said})
            if opening:
                await self._send_text(session, types, opening)

            async def send() -> None:
                while True:
                    chunk = await self._mic.get()
                    if state["opening"]:
                        if not state["done"] or time.monotonic() < state["playing_until"]:
                            # JARVIS is still speaking its opening; don't let it hear itself, but keep the
                            # stream flowing: Google drops a connection that goes quiet mid-reply
                            chunk = bytes(len(chunk))
                        else:
                            state["opening"] = False
                    await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

            def finish_turn() -> None:
                nonlocal conv_id
                user, reply = state["user"].strip(), state["jarvis"].strip()
                state["user"] = state["jarvis"] = ""
                self.turns += 1
                if user or reply:
                    conv_id = self.save_turn(conv_id, user, reply, label)
                    self.conversation_id = conv_id
                    self.emit({"event": "live_turn", "conversation_id": conv_id})
                    if self.after_turn and user and reply:
                        self.after_turn(user, reply, conv_id)
                if _BYE.search(user):
                    state["bye"] = True

            async def receive() -> None:
                while True:
                    async for msg in session.receive():
                        state["heard_at"] = time.monotonic()
                        upd = msg.session_resumption_update
                        if upd and upd.resumable and upd.new_handle:
                            self._handle = upd.new_handle
                        if msg.go_away:
                            log.info("live: server will close the connection soon", extra={"fields": {"time_left": str(msg.go_away.time_left)}})
                        sc = msg.server_content
                        if msg.tool_call:
                            responses = []
                            for fc in msg.tool_call.function_calls or []:
                                self.emit({"event": "live_tool", "tool": fc.name})
                                self.heard = state["user"]
                                state["acted"] = True
                                result = await self.run_tool(fc.name or "", dict(fc.args or {}))
                                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                            await session.send_tool_response(function_responses=responses)
                            state["last"] = time.monotonic()
                        if not sc:
                            continue
                        if sc.interrupted:
                            log.info("live: reply interrupted by audio from the microphone")
                            await self.push_agent({"type": "speaker_flush"})
                            state["playing_until"] = 0.0
                        if sc.input_transcription and sc.input_transcription.text:
                            state["user"] += sc.input_transcription.text
                            state["last"] = time.monotonic()
                            self.emit({"event": "user_text", "text": state["user"].strip()})
                        if sc.output_transcription and sc.output_transcription.text:
                            state["jarvis"] += sc.output_transcription.text
                            state["done"] = False
                            self.emit({"event": "jarvis_text", "text": state["jarvis"].strip()})
                        if sc.model_turn:
                            for part in sc.model_turn.parts or []:
                                if part.inline_data and part.inline_data.data:
                                    if self._barged:
                                        continue  # the user stopped this reply
                                    pcm = part.inline_data.data
                                    now = time.monotonic()
                                    if not state["reply_t0"]:
                                        state["reply_t0"] = now
                                    state["reply_bytes"] += len(pcm)
                                    took = now - state["reply_t0"]
                                    if took >= SLOW_CHECK_S and state["reply_bytes"] < SLOW_RATIO * BYTES_PER_S * took:
                                        raise SlowLine(f"reply audio arrived at {state['reply_bytes'] / took / 1000:.0f} KB/s; "
                                                       f"playing it needs {BYTES_PER_S / 1000:.0f} KB/s")
                                    await self.push_agent({"type": "speaker", "pcm": base64.b64encode(pcm).decode()})
                                    now = time.monotonic()
                                    state["playing_until"] = max(state["playing_until"], now) + len(pcm) / 48000.0
                                    state["last"] = now
                                    state["done"] = False
                        if sc.turn_complete:
                            state["done"] = True
                            state["reply_t0"], state["reply_bytes"] = 0.0, 0
                            if self._barged:
                                self._barged = False
                                state["playing_until"] = 0.0
                                if state["jarvis"].strip():
                                    state["jarvis"] += " ..."
                            finish_turn()

            async def stall() -> None:
                while True:
                    await asyncio.sleep(1.0)
                    if not state["done"] and state["jarvis"] and time.monotonic() - state["heard_at"] > STALL_S:
                        raise LiveError("Google stopped sending mid-reply")

            async def watch() -> str:
                while not self._stop.is_set():
                    await asyncio.sleep(0.5)
                    now = time.monotonic()
                    speaking = now < state["playing_until"]
                    if state["bye"] and not speaking:
                        return "goodbye"
                    if state["reconnecting"] or (not state["done"] and state["jarvis"]):
                        continue  # mid-reply: stall() handles a reply that never finishes
                    if not speaking and now - max(state["last"], state["playing_until"]) > IDLE_S:
                        return "quiet"
                return "stopped"  # the mic button, the pill, or a stop request

            watcher = asyncio.create_task(watch())
            try:
                while True:
                    state["heard_at"] = time.monotonic()
                    talk = [asyncio.create_task(send()), asyncio.create_task(receive()), asyncio.create_task(stall())]
                    done, _ = await asyncio.wait([*talk, watcher], return_when=asyncio.FIRST_COMPLETED)
                    for t in talk:
                        t.cancel()
                    if watcher in done:
                        break  # quiet for a while, or the user said goodbye
                    err = next((t.exception() for t in done if t is not watcher and not t.cancelled() and t.exception()), None)
                    if isinstance(err, SlowLine) or (err is not None and reconnects >= 1):
                        slow = err if isinstance(err, SlowLine) else SlowLine(f"the connection dropped twice ({str(err)[:80]})")
                        break  # this line can't carry live audio: hand over to the offline voice
                    if err is None or reconnects >= RECONNECTS or self._stop.is_set():
                        if err:
                            raise err
                        break
                    # Google dropped the connection: reopen it and carry on with the same conversation.
                    reconnects += 1
                    state["reconnecting"] = True  # a stall before the drop, or the reconnect itself, is not the user going quiet
                    cut_off = not state["done"] and bool(state["jarvis"].strip())
                    log.warning("live connection dropped; reconnecting",
                                extra={"fields": {"attempt": reconnects, "resumable": bool(self._handle), "error": str(err)[:200]}})
                    self.emit({"event": "live_connecting", "origin": "reconnect"})
                    if cut_off:
                        state["jarvis"] += " ..."
                        finish_turn()
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001 - already closed
                        pass
                    while not self._mic.empty():
                        self._mic.get_nowait()  # audio from the gap is stale
                    try:
                        cm, session, model = await self._connect(client, types, [model])
                    finally:
                        state["last"], state["reconnecting"] = time.monotonic(), False
                    self.emit({"event": "live_start", "model": model, "origin": "reconnect"})
                    if cut_off:
                        state["opening"], state["done"] = True, False  # let it finish without hearing itself
                        await self._send_text(session, types, "[The connection dropped while you were speaking. Finish what you were saying, briefly"
                                              + (", without repeating what was already said.]" if self._handle else ".]"))
            finally:
                watcher.cancel()
        except Exception as e:  # noqa: BLE001
            log.exception("live session failed")
            self.emit({"event": "error", "message": f"Live voice stopped: {e}"})
        finally:
            try:
                await self.call_agent("live_mic", {"on": False}, timeout=10)
            except Exception:  # noqa: BLE001 - agent may be gone
                pass
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.emit({"event": "live_end", "conversation_id": conv_id})
            ended = watcher.result() if "watcher" in locals() and watcher.done() and not watcher.cancelled() else "error"
            if slow is not None:
                ended = "too slow"
            log.info("live session closed", extra={"fields": {"reconnects": reconnects, "why": ended}})
            if slow is not None and self.on_slow:
                log.warning("live voice too slow on this connection: %s", slow)
                await self.push_agent({"type": "speaker_flush"})  # the half-arrived reply would only stutter on
                self.on_slow(str(slow), state["user"].strip() if not state["done"] else "", state["acted"], origin)
