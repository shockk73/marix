from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from datetime import datetime

from db import create_watch, get_user_watches, stop_watch as db_stop_watch
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS, DIRECTION_MG_MNSK, DIRECTION_MNSK_MG
from scheduler import start_watch, cancel_watch

router = Router()

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


def _providers_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p.display_name, callback_data=f"prov:{key}")]
        for key, p in PROVIDERS.items()
    ])


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
    await message.answer("Выбери провайдера:", reply_markup=_providers_kb())


@router.callback_query(WatchForm.provider, F.data.startswith("prov:"))
async def on_provider(cb: CallbackQuery, state: FSMContext):
    await state.update_data(provider=cb.data.split(":")[1])
    await state.set_state(WatchForm.direction)
    await cb.message.edit_text("Направление:", reply_markup=_directions_kb())


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

    watch_id = await create_watch(
        user_id=message.from_user.id,
        provider=data["provider"],
        direction=data["direction"],
        date=data["date"],
        time_from=data["time_from"],
        time_to=data["time_to"],
        interval_sec=interval,
    )
    watch = {
        "id": watch_id,
        "user_id": message.from_user.id,
        "notified_trips": "[]",
        **data,
        "interval_sec": interval,
    }
    await start_watch(watch)

    p = PROVIDERS[data["provider"]]
    dir_label = DIRECTION_LABELS[data["direction"]]
    await message.answer(
        f"Отслеживание #{watch_id} запущено!\n"
        f"{p.display_name} | {dir_label}\n"
        f"{data['date']}, {data['time_from']}–{data['time_to']}\n"
        f"Интервал: {interval} сек\n\n"
        f"Остановить: /stop {watch_id}"
    )


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
