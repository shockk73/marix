from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from datetime import datetime

from config import AUTH_CODE
from db import (
    MAX_AUTH_ATTEMPTS,
    authorize_user, create_watch, get_user_watches, increment_failed_attempts,
    is_authorized, is_banned, stop_watch as db_stop_watch,
)
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS, DIRECTION_MG_MNSK, DIRECTION_MNSK_MG
from scheduler import start_watch, cancel_watch
from llm.agent import LLMAgent

router = Router()

_agent: LLMAgent | None = None


def set_agent(agent: LLMAgent) -> None:
    global _agent
    _agent = agent


def _user_display_name(u) -> str | None:
    return (u.first_name or u.username or None) if u else None


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if await is_authorized(user.id):
            return await handler(event, data)

        if await is_banned(user.id):
            return None

        if isinstance(event, Message) and event.text:
            text = event.text.strip()
            if text.startswith("/auth"):
                parts = text.split(maxsplit=1)
                code = parts[1].strip() if len(parts) > 1 else ""
                if not code:
                    await event.answer("Использование: /auth <код>")
                    return None
                if code == AUTH_CODE:
                    await authorize_user(user.id)
                    state: FSMContext | None = data.get("state")
                    if state is not None:
                        await state.clear()
                    await event.answer("Доступ разрешён. /start — начать.")
                    return None
                failed = await increment_failed_attempts(user.id)
                remaining = MAX_AUTH_ATTEMPTS - failed
                if remaining <= 0:
                    await event.answer("Неверный код. Вы заблокированы.")
                else:
                    await event.answer(f"Неверный код. Осталось попыток: {remaining}")
                return None

        if isinstance(event, Message):
            await event.answer("Доступ ограничен. Авторизуйся: /auth <код>")
        elif isinstance(event, CallbackQuery):
            await event.answer("Нужен доступ: /auth <код>", show_alert=True)
        return None


router.message.outer_middleware(AuthMiddleware())
router.callback_query.outer_middleware(AuthMiddleware())

_DIRECTIONS = [
    (DIRECTION_MG_MNSK, DIRECTION_LABELS[DIRECTION_MG_MNSK]),
    (DIRECTION_MNSK_MG, DIRECTION_LABELS[DIRECTION_MNSK_MG]),
]


class WatchForm(StatesGroup):
    provider = State()
    direction = State()
    date = State()
    time_from = State()
    time_to = State()
    interval = State()


def _providers_kb(selected: set[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{'☑️' if key in selected else '☐'} {p.display_name}",
            callback_data=f"prov:{key}",
        )]
        for key, p in PROVIDERS.items()
    ]
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="prov_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _directions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"dir:{key}")]
        for key, label in _DIRECTIONS
    ])


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Слежу за местами в маршрутках.\n\n"
        "/watch — создать отслеживание\n"
        "/list — активные задачи\n"
        "/stop <id> — остановить задачу"
    )


@router.message(Command("watch"))
async def cmd_watch(message: Message, state: FSMContext):
    await state.set_state(WatchForm.provider)
    await state.update_data(providers=[])
    await message.answer(
        "Выбери провайдеров (можно несколько), затем «Готово»:",
        reply_markup=_providers_kb(set()),
    )


@router.callback_query(WatchForm.provider, F.data.startswith("prov:"))
async def on_provider_toggle(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("providers", []))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    await state.update_data(providers=list(selected))
    await cb.message.edit_reply_markup(reply_markup=_providers_kb(selected))
    await cb.answer()


@router.callback_query(WatchForm.provider, F.data == "prov_done")
async def on_providers_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("providers", [])
    if not selected:
        await cb.answer("Выбери хотя бы одного провайдера", show_alert=True)
        return
    await state.set_state(WatchForm.direction)
    await cb.message.edit_text("Направление:", reply_markup=_directions_kb())
    await cb.answer()


@router.callback_query(WatchForm.direction, F.data.startswith("dir:"))
async def on_direction(cb: CallbackQuery, state: FSMContext):
    await state.update_data(direction=cb.data.split(":")[1])
    await state.set_state(WatchForm.date)
    await cb.message.edit_text("Введи дату (ГГГГ-ММ-ДД), например 2026-05-24:")


@router.message(WatchForm.date)
async def on_date(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат. Пример: 2026-05-24")
        return
    await state.update_data(date=text)
    await state.set_state(WatchForm.time_from)
    await message.answer("Время ОТ (ЧЧ:ММ), например 11:00:")


@router.message(WatchForm.time_from)
async def on_time_from(message: Message, state: FSMContext):
    t = message.text.strip()
    if not _valid_time(t):
        await message.answer("Неверный формат. Пример: 11:00")
        return
    await state.update_data(time_from=t)
    await state.set_state(WatchForm.time_to)
    await message.answer("Время ДО (ЧЧ:ММ), например 23:00:")


@router.message(WatchForm.time_to)
async def on_time_to(message: Message, state: FSMContext):
    t = message.text.strip()
    if not _valid_time(t):
        await message.answer("Неверный формат. Пример: 23:00")
        return
    await state.update_data(time_to=t)
    await state.set_state(WatchForm.interval)
    await message.answer("Интервал проверки в секундах (минимум 60), например 120:")


@router.message(WatchForm.interval)
async def on_interval(message: Message, state: FSMContext):
    try:
        interval = int(message.text.strip())
        if interval < 60:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число секунд (минимум 60):")
        return

    data = await state.get_data()
    await state.clear()

    selected = data.get("providers", [])
    if not selected:
        await message.answer("Не выбрано ни одного провайдера. /watch — заново.")
        return

    created = []
    for provider_key in selected:
        watch_id = await create_watch(
            user_id=message.from_user.id,
            provider=provider_key,
            direction=data["direction"],
            date=data["date"],
            time_from=data["time_from"],
            time_to=data["time_to"],
            interval_sec=interval,
        )
        await start_watch({
            "id": watch_id,
            "user_id": message.from_user.id,
            "notified_trips": "[]",
            "provider": provider_key,
            "direction": data["direction"],
            "date": data["date"],
            "time_from": data["time_from"],
            "time_to": data["time_to"],
            "interval_sec": interval,
        })
        p = PROVIDERS[provider_key]
        created.append(f"#{watch_id} {p.display_name} (/stop {watch_id})")

    dir_label = DIRECTION_LABELS[data["direction"]]
    lines = [f"Запущено отслеживаний: {len(created)}"]
    lines.extend(created)
    lines.append("")
    lines.append(f"{dir_label}, {data['date']}, {data['time_from']}–{data['time_to']}, каждые {interval}с")
    await message.answer("\n".join(lines))


@router.message(Command("list"))
async def cmd_list(message: Message):
    watches = await get_user_watches(message.from_user.id)
    if not watches:
        await message.answer("Нет активных отслеживаний.")
        return
    lines = ["Активные отслеживания:\n"]
    for w in watches:
        p = PROVIDERS[w["provider"]]
        dir_label = DIRECTION_LABELS[w["direction"]]
        lines.append(
            f"#{w['id']} {p.display_name} | {dir_label}\n"
            f"  {w['date']}, {w['time_from']}–{w['time_to']}, каждые {w['interval_sec']}с\n"
            f"  /stop {w['id']}"
        )
    await message.answer("\n\n".join(lines))


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /stop <id>")
        return
    watch_id = int(parts[1])
    if await db_stop_watch(watch_id, message.from_user.id):
        await cancel_watch(watch_id)
        await message.answer(f"Отслеживание #{watch_id} остановлено.")
    else:
        await message.answer(f"Отслеживание #{watch_id} не найдено.")


def _valid_time(t: str) -> bool:
    if len(t) != 5 or t[2] != ":":
        return False
    try:
        datetime.strptime(t, "%H:%M")
        return True
    except ValueError:
        return False


@router.message(F.photo)
async def on_photo(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    if _agent is None:
        return
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)
    image_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
    await _agent.handle_photo(
        user_id=message.from_user.id,
        image_bytes=image_bytes,
        mime="image/jpeg",
        caption=message.caption,
        user_name=_user_display_name(message.from_user),
    )


@router.message(F.voice | F.audio)
async def on_voice_or_audio(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    if _agent is None:
        return
    if message.voice:
        file_id = message.voice.file_id
        audio_format = "ogg"
    else:
        file_id = message.audio.file_id
        mime = (message.audio.mime_type or "").lower()
        if "mp3" in mime or "mpeg" in mime:
            audio_format = "mp3"
        elif "wav" in mime:
            audio_format = "wav"
        elif "ogg" in mime or "opus" in mime:
            audio_format = "ogg"
        else:
            audio_format = "mp3"
    file = await message.bot.get_file(file_id)
    buf = await message.bot.download_file(file.file_path)
    audio_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
    await _agent.handle_audio(
        user_id=message.from_user.id,
        audio_bytes=audio_bytes,
        audio_format=audio_format,
        caption=message.caption,
        user_name=_user_display_name(message.from_user),
    )


@router.message(F.text)
async def on_text_fallback(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    if message.text and message.text.startswith("/"):
        return
    if _agent is None:
        await message.answer("AI пока не настроен.")
        return
    await _agent.run_turn(
        user_id=message.from_user.id,
        text=message.text,
        user_name=_user_display_name(message.from_user),
    )


@router.callback_query(F.data.startswith("ai:"))
async def on_ai_callback(cb: CallbackQuery):
    if _agent is None:
        await cb.answer()
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    user_id = cb.from_user.id

    import json as _json
    from db import get_pending_tool_call
    pending = await get_pending_tool_call(user_id)
    if pending is None:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Вопрос устарел.", show_alert=False)
        return

    try:
        options = _json.loads(pending["options_json"])
    except Exception:
        options = []
    if not (0 <= idx < len(options)):
        await cb.answer()
        return

    selected = options[idx]
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer()
    await _agent.continue_turn(user_id=user_id, selected_option=selected)
