"""Phone: talk to JARVIS from Telegram, anywhere. The user makes their own bot with @BotFather, pastes its token in
Customize > Phone, and sends the bot the pairing code shown there. From then on that one chat (and no other) is the
owner: its messages run like messages typed in the app, steps that need a yes arrive as Approve / Decline buttons,
and automations can send their results to it.

No server or open port is needed: JARVIS asks Telegram for new messages (long polling). The token is stored
encrypted with the other keys and never appears in logs or errors (it is part of every Telegram address)."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("jarvis.phone")

API = "https://api.telegram.org"
SECRET = "phone:telegram_token"
PAIR_S = 15 * 60  # a pairing code works this long
POLL_S = 25
MAX_TEXT = 4000  # Telegram's limit is 4096
TOKEN_RX = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")


class PhoneError(Exception):
    pass


class Phone:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.on_message: Callable[[str], Awaitable[None]] | None = None  # set by the router
        self.on_confirm: Callable[[str, bool], bool] | None = None  # (call id, approved) -> was it still waiting
        self.code = ""
        self.code_until = 0.0
        self.bot = ""
        self.error = ""
        self.offset = 0
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # ---------- state ----------

    def _token(self) -> str:
        return self.settings.service_key(SECRET)

    def _link(self) -> dict[str, Any]:
        return self.settings.get("phone") or {}

    @property
    def linked(self) -> bool:
        return bool(self._token() and self._link().get("chat_id"))

    def status(self) -> dict[str, Any]:
        link = self._link()
        pairing = bool(self._token()) and not link.get("chat_id")
        if pairing and time.time() > self.code_until:
            self.new_code()
        return {"has_token": bool(self._token()), "bot": self.bot, "linked": self.linked, "who": link.get("who", ""),
                "pairing_code": self.code if pairing else "", "error": self.error}

    def new_code(self) -> str:
        self.code = f"{secrets.randbelow(10**6):06d}"
        self.code_until = time.time() + PAIR_S
        return self.code

    # ---------- Telegram ----------

    def _clean(self, text: str) -> str:
        token = self._token()
        return text.replace(token, "***") if token else text

    async def _api(self, method: str, token: str | None = None, timeout: float = 20, **params: Any) -> Any:
        token = token or self._token()
        if not token:
            raise PhoneError("No Telegram bot is set up.")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout + 10, connect=10)) as c:
                r = await c.post(f"{API}/bot{token}/{method}", json=params)
        except httpx.HTTPError as e:
            raise PhoneError(f"Could not reach Telegram ({type(e).__name__}).") from None  # its text holds the address, and so the token
        try:
            data = r.json()
        except ValueError:
            raise PhoneError(f"Telegram answered HTTP {r.status_code}.") from None
        if not data.get("ok"):
            raise PhoneError(self._clean(str(data.get("description") or f"Telegram refused ({r.status_code})."))[:200])
        return data.get("result")

    async def set_token(self, token: str) -> dict[str, Any]:
        token = token.strip()
        if not TOKEN_RX.match(token):
            raise PhoneError("That doesn't look like a bot token. @BotFather gives one like 123456789:AA... ")
        me = await self._api("getMe", token=token)
        await self._api("deleteWebhook", token=token, drop_pending_updates=True)  # polling only works without a webhook
        self.settings.set_service_key(SECRET, token)
        self.settings.set_phone({})
        self.bot, self.error, self.offset = me.get("username", ""), "", 0
        self.new_code()
        self._wake.set()
        return self.status()

    def unlink(self) -> None:
        self.settings.set_service_key(SECRET, "")
        self.settings.set_phone({})
        self.bot, self.code, self.error = "", "", ""
        self._wake.set()

    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int | None:
        """Send to the owner's chat; buttons are (label, callback data). Returns the message id."""
        chat = self._link().get("chat_id")
        if not chat:
            return None
        text = text.strip() or "(nothing to say)"
        last = None
        for i in range(0, len(text), MAX_TEXT):
            params: dict[str, Any] = {"chat_id": chat, "text": text[i:i + MAX_TEXT], "disable_web_page_preview": True}
            if buttons and i + MAX_TEXT >= len(text):
                params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
            last = (await self._api("sendMessage", **params) or {}).get("message_id")
        return last

    async def typing(self) -> None:
        chat = self._link().get("chat_id")
        if chat:
            try:
                await self._api("sendChatAction", chat_id=chat, action="typing")
            except PhoneError:
                pass

    async def ask_approval(self, call_id: str, what: str) -> None:
        await self.send(f"Needs your OK: {what}\n(Declined on its own after 2 minutes.)", [("✅ Approve", f"y:{call_id}"), ("✖ Decline", f"n:{call_id}")])

    # ---------- incoming ----------

    async def handle(self, update: dict[str, Any]) -> None:
        link = self._link()
        if "callback_query" in update:
            q = update["callback_query"]
            if not link.get("chat_id") or (q.get("message") or {}).get("chat", {}).get("id") != link["chat_id"]:
                return
            data = str(q.get("data") or "")
            answer = "That request is no longer waiting."
            if data[:2] in ("y:", "n:") and self.on_confirm:
                approved = data[0] == "y"
                if self.on_confirm(data[2:], approved):
                    answer = "Approved." if approved else "Declined."
            await self._api("answerCallbackQuery", callback_query_id=q["id"], text=answer)
            msg = q.get("message") or {}
            if msg.get("message_id"):
                try:
                    await self._api("editMessageText", chat_id=link["chat_id"], message_id=msg["message_id"],
                                    text=f"{msg.get('text', '')}\n\n→ {answer}")
                except PhoneError:
                    pass
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return  # never in groups: anyone there could give orders
        text = str(msg.get("text") or "").strip()
        if not link.get("chat_id"):  # pairing: only the right code, in time, links a chat
            if self.code and time.time() <= self.code_until and self.code in text:
                who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "you"
                self.settings.set_phone({"chat_id": chat["id"], "who": who, "linked_at": time.time()})
                self.code = ""
                log.info("phone linked", extra={"fields": {"who": who}})
                await self.send(f"Linked. Hello {who}, this chat now talks to JARVIS on your PC.\n"
                                "Send any request. /stop stops what it's doing, /new starts a fresh conversation.")
            return
        if chat.get("id") != link["chat_id"]:
            return  # someone else found the bot: ignored, and told nothing
        if not text:
            await self.send("Only text messages for now.")
            return
        if self.on_message:  # its own task: the loop must stay free to receive the Approve tap the request may wait for
            task = asyncio.create_task(self.on_message(text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        while True:
            if not self._token():
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                if not self.bot:
                    self.bot = (await self._api("getMe") or {}).get("username", "")
                updates = await self._api("getUpdates", timeout=POLL_S, offset=self.offset, allowed_updates=["message", "callback_query"])
                self.error = ""
                for u in updates or []:
                    self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                    try:
                        await self.handle(u)
                    except Exception:  # noqa: BLE001 - one bad message must not stop the link
                        log.exception("phone message failed")
            except PhoneError as e:
                self.error = str(e)
                log.warning("phone link: %s", e)
                await asyncio.sleep(15)
            except Exception:  # noqa: BLE001
                log.exception("phone link loop failed")
                await asyncio.sleep(15)
