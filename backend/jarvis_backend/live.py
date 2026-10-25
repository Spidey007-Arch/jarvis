"""Native speech-to-speech with Gemini Live.

The agent streams the microphone here (16 kHz PCM); this relays it to a Gemini Live session and sends
the spoken reply (24 kHz PCM) back to the agent's speakers as it arrives. Gemini hears the user
directly, answers in its own voice, notices when it is interrupted and can call JARVIS's tools.
Both sides are transcribed, and every finished exchange is saved as a normal conversation.

A session opens on the wake word or push-to-talk and closes after IDLE_S of quiet, or when the user
says something like "that's all". It talks to Google directly with the saved Gemini key (OmniRoute
does not relay live audio)."""
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
_BYE = re.compile(r"\b(that'?s all|that will be all|that'?ll be all|goodbye|good night|bye jarvis|stop listening|thank you,? that'?s it)\b", re.I)


class LiveError(Exception):
    pass


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
        save_turn: Callable[[str | None, str, str], str],
        after_turn: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.push_agent = push_agent
        self.call_agent = call_agent
        self.emit = emit
        self.system_prompt = system_prompt
        self.tools = tools
        self.run_tool = run_tool
        self.save_turn = save_turn
        self.after_turn = after_turn
        self._task: asyncio.Task | None = None
        self._mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)  # ~4 s; older audio is dropped on a slow line
        self._stop = asyncio.Event()
        self._available: list[str] | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    # ----- control -----

    async def start(self, origin: str) -> dict[str, Any]:
        if self.active:
            return {"live": True, "already": True}
        if not self.api_key():
            raise LiveError("Live voice needs a Google Gemini key saved in Settings (OmniRoute cannot relay live audio).")
        self._stop = asyncio.Event()
        self._mic = asyncio.Queue(maxsize=100)
        self._task = asyncio.create_task(self._run(origin))
        return {"live": True}

    async def stop(self) -> None:
        self._stop.set()

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

    def _config(self, types: Any, voice: bool) -> Any:
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in self.tools]
        kw: dict[str, Any] = dict(
            response_modalities=["AUDIO"],
            system_instruction=self.system_prompt(),
            tools=[types.Tool(function_declarations=decls)],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        if voice:
            kw["speech_config"] = types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)))
        return types.LiveConnectConfig(**kw)

    async def _connect(self, client: Any, types: Any) -> tuple[Any, Any, str]:
        last: Exception | None = None
        for model in self._models(client):
            for voice in (True, False):  # some models refuse a voice choice; then use their default
                try:
                    cm = client.aio.live.connect(model=model, config=self._config(types, voice))
                    session = await asyncio.wait_for(cm.__aenter__(), 20)
                    return cm, session, model
                except Exception as e:  # noqa: BLE001
                    last = e
                    log.warning("live connect failed", extra={"fields": {"model": model, "voice": voice, "error": str(e)[:200]}})
        raise LiveError(f"Could not open a live voice session: {last}")

    async def _run(self, origin: str) -> None:
        from google import genai
        from google.genai import types

        conv_id: str | None = None
        try:
            client = genai.Client(api_key=self.api_key())
            self.emit({"event": "live_connecting", "origin": origin})
            cm, session, model = await self._connect(client, types)
        except Exception as e:  # noqa: BLE001
            self.emit({"event": "error", "message": str(e)})
            return
        log.info("live session open", extra={"fields": {"model": model, "origin": origin}})
        state = {"last": time.monotonic(), "playing_until": 0.0, "user": "", "jarvis": "", "bye": False}
        try:
            await self.call_agent("live_mic", {"on": True}, timeout=10)
            self.emit({"event": "live_start", "model": model})

            async def send() -> None:
                while True:
                    chunk = await self._mic.get()
                    await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

            async def receive() -> None:
                nonlocal conv_id
                while True:
                    async for msg in session.receive():
                        sc = msg.server_content
                        if msg.tool_call:
                            responses = []
                            for fc in msg.tool_call.function_calls or []:
                                self.emit({"event": "live_tool", "tool": fc.name})
                                result = await self.run_tool(fc.name or "", dict(fc.args or {}))
                                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                            await session.send_tool_response(function_responses=responses)
                            state["last"] = time.monotonic()
                        if not sc:
                            continue
                        if sc.interrupted:
                            await self.push_agent({"type": "speaker_flush"})
                            state["playing_until"] = 0.0
                        if sc.input_transcription and sc.input_transcription.text:
                            state["user"] += sc.input_transcription.text
                            state["last"] = time.monotonic()
                            self.emit({"event": "user_text", "text": state["user"].strip()})
                        if sc.output_transcription and sc.output_transcription.text:
                            state["jarvis"] += sc.output_transcription.text
                            self.emit({"event": "jarvis_text", "text": state["jarvis"].strip()})
                        if sc.model_turn:
                            for part in sc.model_turn.parts or []:
                                if part.inline_data and part.inline_data.data:
                                    pcm = part.inline_data.data
                                    await self.push_agent({"type": "speaker", "pcm": base64.b64encode(pcm).decode()})
                                    now = time.monotonic()
                                    state["playing_until"] = max(state["playing_until"], now) + len(pcm) / 48000.0
                                    state["last"] = now
                        if sc.turn_complete:
                            user, reply = state["user"].strip(), state["jarvis"].strip()
                            state["user"] = state["jarvis"] = ""
                            if user or reply:
                                conv_id = self.save_turn(conv_id, user, reply)
                                self.emit({"event": "live_turn", "conversation_id": conv_id})
                                if self.after_turn and user and reply:
                                    self.after_turn(user, reply, conv_id)
                            if _BYE.search(user):
                                state["bye"] = True

            async def watch() -> None:
                while not self._stop.is_set():
                    await asyncio.sleep(0.5)
                    now = time.monotonic()
                    speaking = now < state["playing_until"]
                    if state["bye"] and not speaking:
                        return
                    if not speaking and now - max(state["last"], state["playing_until"]) > IDLE_S:
                        return

            tasks = [asyncio.create_task(send()), asyncio.create_task(receive()), asyncio.create_task(watch())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]
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
            log.info("live session closed")
