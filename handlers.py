from typing import Any, Awaitable, Callable
from uuid import uuid4

from aiogram import BaseMiddleware, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup,
)
from datetime import datetime

from config import AUTH_CODE
from db import (
    MAX_AUTH_ATTEMPTS,
    authorize_user, clear_chat_session, create_watch, get_active_goal_booking,
    get_active_watches, get_site_credentials, get_user_name, get_user_role,
    get_user_watches, get_watch, increment_failed_attempts, is_authorized,
    is_banned, set_user_role, stop_watch as db_stop_watch, use_invite,
)
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS
from scheduler import start_watch, cancel_watch
from llm.agent import LLMAgent, chosen_markup

router = Router()

_agent: LLMAgent | None = None


def set_agent(agent: LLMAgent) -> None:
    global _agent
    _agent = agent


def _user_display_name(u) -> str | None:
    return (u.first_name or u.username or None) if u else None


WELCOME_AFTER_INVITE = (
    "Доступ открыт! Я слежу за свободными местами в маршрутках Беларуси.\n"
    "Напиши, что нужно — например: «следи за Минск → Барановичи завтра утром»."
)
ADMIN_GREETING = "Ты админ. Напиши «дай инвайт», чтобы пригласить человека."
INVITE_REJECT = ("Ссылка не работает или уже использована. "
                 "Попроси новую у того, кто тебя пригласил.")
ONBOARDING_TRIGGER = ("[новый пользователь вошёл по инвайту — поздоровайся "
                      "и покажи стартовый экран]")

BTN_WATCH = "🔍 Следить за местами"
BTN_LIST = "📋 Мои слежки"
BTN_HELP = "❓ Что ты умеешь"
BTN_ADMIN = "🛠 Админка"


def main_reply_kb(role: str | None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_WATCH), KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if role == "admin":
        rows[1].append(KeyboardButton(text=BTN_ADMIN))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               is_persistent=True)


async def handle_unauthorized_message(
    user_id: int, text: str,
) -> tuple[str, bool, bool] | None:
    """Возвращает (ответ, авторизован_ли, вошёл_ли_по_инвайту) или None —
    стандартный отказ. Подбор инвайт-токена НЕ инкрементит счётчик бана,
    подбор /auth — да."""
    text = text.strip()
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token:
            return None
        if await use_invite(token, user_id):
            return (WELCOME_AFTER_INVITE, True, True)
        return (INVITE_REJECT, False, False)
    if text == AUTH_CODE:
        await authorize_user(user_id, role="admin")
        return (ADMIN_GREETING, True, False)
    if text.startswith("/auth"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            return ("Использование: /auth <код>", False, False)
        if code == AUTH_CODE:
            await authorize_user(user_id, role="admin")
            return (ADMIN_GREETING, True, False)
        failed = await increment_failed_attempts(user_id)
        remaining = MAX_AUTH_ATTEMPTS - failed
        if remaining <= 0:
            return ("Неверный код. Вы заблокированы.", False, False)
        return (f"Неверный код. Осталось попыток: {remaining}", False, False)
    return None


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
            result = await handle_unauthorized_message(user.id, event.text)
            if result is not None:
                reply, authorized, invited = result
                if not authorized:
                    await event.answer(reply)
                    return None
                state: FSMContext | None = data.get("state")
                if state is not None:
                    await state.clear()
                role = await get_user_role(user.id)
                await event.answer(reply, reply_markup=main_reply_kb(role))
                if invited and _agent is not None:
                    await _agent.run_turn(
                        user_id=user.id,
                        text=ONBOARDING_TRIGGER,
                        user_name=_user_display_name(user),
                    )
                return None

        if isinstance(event, Message):
            await event.answer("Доступ ограничен. Авторизуйся: /auth <код>")
        elif isinstance(event, CallbackQuery):
            await event.answer("Нужен доступ: /auth <код>", show_alert=True)
        return None


router.message.outer_middleware(AuthMiddleware())
router.callback_query.outer_middleware(AuthMiddleware())


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


def _supported_directions(provider_keys: list[str]) -> list[tuple[str, str]]:
    common = set(DIRECTION_LABELS)
    for provider_key in provider_keys:
        provider = PROVIDERS.get(provider_key)
        if provider is None:
            common = set()
            break
        common &= set(provider.directions)
    return [
        (key, label)
        for key, label in DIRECTION_LABELS.items()
        if key in common
    ]


def _directions_kb(provider_keys: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"dir:{key}")]
        for key, label in _supported_directions(provider_keys)
    ])


@router.message(Command("start"))
async def cmd_start(message: Message):
    role = await get_user_role(message.from_user.id)
    await message.answer(
        "Привет! Слежу за местами в маршрутках. Просто напиши, что нужно.\n\n"
        "Команды-фоллбек: /watch, /list, /stop <id>, /reset",
        reply_markup=main_reply_kb(role),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    """Очистка LLM-сессии: своей — любому, чужой (/reset <id>) — админу."""
    parts = (message.text or "").split()
    target = message.from_user.id
    if len(parts) > 1:
        if not parts[1].isdigit():
            await message.answer("Использование: /reset [user_id]")
            return
        if await get_user_role(message.from_user.id) != "admin":
            await message.answer("Чужую сессию может чистить только админ.")
            return
        target = int(parts[1])
    await clear_chat_session(target)
    suffix = f" (юзер {target})" if target != message.from_user.id else ""
    await message.answer(f"Сессия очищена — начинаем с чистого листа.{suffix}")


@router.message(Command("auth"))
async def cmd_auth(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if code == AUTH_CODE:
        await set_user_role(message.from_user.id, "admin")
        await message.answer(ADMIN_GREETING)
    else:
        await message.answer("Неверный код.")


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
    if not _supported_directions(selected):
        await cb.answer("У выбранных провайдеров нет общих направлений", show_alert=True)
        return
    await state.set_state(WatchForm.direction)
    await cb.message.edit_text("Направление:", reply_markup=_directions_kb(selected))
    await cb.answer()


@router.callback_query(WatchForm.direction, F.data.startswith("dir:"))
async def on_direction(cb: CallbackQuery, state: FSMContext):
    direction = cb.data.split(":", 1)[1]
    data = await state.get_data()
    selected = data.get("providers", [])
    if direction not in {key for key, _ in _supported_directions(selected)}:
        await cb.answer("Это направление недоступно у выбранных провайдеров", show_alert=True)
        return
    await state.update_data(direction=direction)
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

    direction = data["direction"]
    unsupported = [
        provider_key
        for provider_key in selected
        if direction not in PROVIDERS[provider_key].directions
    ]
    if unsupported:
        names = ", ".join(PROVIDERS[key].display_name for key in unsupported)
        await message.answer(
            f"Направление {DIRECTION_LABELS[direction]} недоступно у: {names}. /watch — заново."
        )
        return

    created = []
    goal_id = uuid4().hex[:12]
    for provider_key in selected:
        watch_id = await create_watch(
            user_id=message.from_user.id,
            provider=provider_key,
            direction=direction,
            date=data["date"],
            time_from=data["time_from"],
            time_to=data["time_to"],
            interval_sec=interval,
            goal_id=goal_id,
        )
        await start_watch({
            "id": watch_id,
            "user_id": message.from_user.id,
            "notified_trips": "[]",
            "provider": provider_key,
            "direction": direction,
            "date": data["date"],
            "time_from": data["time_from"],
            "time_to": data["time_to"],
            "interval_sec": interval,
            "autobook": "off",
            "goal_id": goal_id,
            "pref_time_from": None,
            "pref_time_to": None,
        })
        p = PROVIDERS[provider_key]
        created.append(f"#{watch_id} {p.display_name} (/stop {watch_id})")

    dir_label = DIRECTION_LABELS[direction]
    lines = [f"Запущено отслеживаний: {len(created)}"]
    lines.extend(created)
    lines.append("")
    lines.append(f"{dir_label}, {data['date']}, {data['time_from']}–{data['time_to']}, каждые {interval}с")
    await message.answer("\n".join(lines))


def _mask_phone(phone: str) -> str:
    return f"{phone[:4]}…{phone[-2:]}" if len(phone) >= 6 else phone


def _clicked_label(cb: CallbackQuery) -> str | None:
    """Текст нажатой кнопки из клавиатуры сообщения."""
    try:
        for row in cb.message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data == cb.data:
                    return button.text
    except AttributeError:
        pass
    return None


async def _mark_chosen(cb: CallbackQuery, label: str | None) -> None:
    """Заменяет клавиатуру следом выбора — что нажал юзер, остаётся видно."""
    try:
        if label:
            await cb.message.edit_reply_markup(reply_markup=chosen_markup(label))
        else:
            await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


def _watch_line(w: dict, creds_connected: bool = False) -> str:
    p = PROVIDERS[w["provider"]]
    dir_label = DIRECTION_LABELS[w["direction"]]
    extra = ""
    if w["provider"] == "baranovichi_express":
        mode = w.get("autobook") or "off"
        if mode == "auto":
            ab = "автобронь: ⚡ бронирую сам"
        elif mode == "confirm":
            ab = "автобронь: спрошу кнопкой"
        elif creds_connected:
            ab = "автобронь: выкл (в уведомлении будет кнопка брони)"
        else:
            ab = "автобронь: выкл (аккаунт не подключён)"
        extra += f"\n  {ab}"
    if w.get("pref_time_from"):
        extra += f"\n  приоритет: {w['pref_time_from']}–{w['pref_time_to']}"
    if w.get("pickup_stop") or w.get("dropoff_stop"):
        extra += (f"\n  остановки: {w.get('pickup_stop') or 'главная'} → "
                  f"{w.get('dropoff_stop') or 'главная'}")
    return (
        f"#{w['id']} {p.display_name} | {dir_label}\n"
        f"  {w['date']}, {w['time_from']}–{w['time_to']}, "
        f"каждые {w['interval_sec']}с{extra}"
    )


def _account_line(creds: dict | None) -> str:
    if creds is None:
        return "Аккаунт Барановичи Экспресс: не подключён"
    return f"Аккаунт Барановичи Экспресс: подключён ({_mask_phone(creds['phone'])})"


@router.message(Command("list"))
async def cmd_list(message: Message):
    # админ видит полный отчёт по всем пользователям
    if await get_user_role(message.from_user.id) == "admin":
        watches = await get_active_watches()
        if not watches:
            await message.answer("Активных отслеживаний нет ни у кого.")
            return
        by_user: dict[int, list[dict]] = {}
        for w in watches:
            by_user.setdefault(w["user_id"], []).append(w)
        lines = [f"Все активные отслеживания ({len(watches)}):"]
        for uid, user_watches in by_user.items():
            name = await get_user_name(uid) or "без имени"
            creds = await get_site_credentials(uid)
            acc = f", аккаунт {_mask_phone(creds['phone'])}" if creds else ""
            lines.append(f"\n👤 {name} (id {uid}{acc}):")
            for w in user_watches:
                lines.append(_watch_line(w, creds_connected=creds is not None))
        await message.answer("\n".join(lines))
        return

    creds = await get_site_credentials(message.from_user.id)
    watches = await get_user_watches(message.from_user.id)
    if not watches:
        await message.answer(
            f"Нет активных отслеживаний.\n\n{_account_line(creds)}")
        return
    lines = ["Активные отслеживания:\n"]
    for w in watches:
        lines.append(
            f"{_watch_line(w, creds_connected=creds is not None)}\n"
            f"  /stop {w['id']}")
    lines.append(_account_line(creds))
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
    if message.text and message.text.strip() == AUTH_CODE:
        # код не должен утекать в историю LLM
        await set_user_role(message.from_user.id, "admin")
        await message.answer(ADMIN_GREETING)
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
    if not isinstance(options, list) or not (0 <= idx < len(options)):
        # dict в options_json — это pending-подтверждение (aic:), не наш формат
        await cb.answer()
        return

    selected = options[idx]
    await _mark_chosen(cb, _clicked_label(cb) or selected)
    await cb.answer()
    await _agent.continue_turn(user_id=user_id, selected_option=selected)


@router.callback_query(F.data == "chosen")
async def on_chosen_noop(cb: CallbackQuery):
    await cb.answer("Уже выбрано ✓")


@router.callback_query(F.data.startswith("aim:"))
async def on_ai_form_answer(cb: CallbackQuery):
    """Кнопка одного из вопросов формы ask_user_form."""
    if _agent is None:
        await cb.answer()
        return
    parts = cb.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await cb.answer()
        return
    outcome = await _agent.answer_form_option(
        cb.from_user.id, int(parts[1]), int(parts[2]))
    if outcome == "stale":
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Вопрос устарел.", show_alert=False)
    elif outcome == "dup":
        await cb.answer("На этот вопрос уже отвечено.")
    elif outcome == "recorded":
        await cb.answer("Принял ✔")
    else:
        await cb.answer("Все ответы получены ✔")


@router.callback_query(F.data.startswith("aic:"))
async def on_ai_confirm(cb: CallbackQuery):
    """Кнопки «Да/Нет» системного подтверждения state-changing инструмента."""
    if _agent is None:
        await cb.answer()
        return
    from db import get_pending_tool_call
    pending = await get_pending_tool_call(cb.from_user.id)
    if pending is None:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Вопрос устарел.", show_alert=False)
        return
    approved = cb.data == "aic:yes"
    await _mark_chosen(cb, "Да" if approved else "Нет")
    await cb.answer("Выполняю…" if approved else "Отменил.")
    await _agent.resolve_confirmation(user_id=cb.from_user.id, approved=approved)


@router.callback_query(F.data.startswith("bk:"))
async def on_booking_button(cb: CallbackQuery):
    """Кнопки брони из уведомлений: bk:b — забронировать, bk:r — перебронировать."""
    from booking_flow import execute_booking

    parts = cb.data.split(":", 3)
    if len(parts) != 4 or not parts[2].isdigit():
        await cb.answer()
        return
    kind, watch_id, dep_time = parts[1], int(parts[2]), parts[3]
    user_id = cb.from_user.id

    watch = await get_watch(watch_id)
    if watch is None or watch["user_id"] != user_id:
        await cb.answer("Слежка не найдена.", show_alert=True)
        return

    booking = await get_active_goal_booking(user_id, watch.get("goal_id"))
    rebook_from = None
    if kind == "r":
        if booking is not None and booking["departure_time"] == dep_time:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await cb.answer("Уже забронировано ✅", show_alert=False)
            return
        rebook_from = booking
    else:
        if booking is not None:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await cb.answer(
                f"Уже есть бронь на {booking['departure_time']} ✅",
                show_alert=False,
            )
            return

    await _mark_chosen(cb, _clicked_label(cb))
    await cb.answer("Бронирую…")
    _, text, _ = await execute_booking(
        user_id=user_id,
        date=watch["date"],
        direction=watch["direction"],
        departure_time=dep_time,
        watch=watch,
        rebook_from=rebook_from,
    )
    await cb.message.answer(text)
