import asyncio
import csv
import io
import os
import sqlite3
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

# Configuration comes only from Bothost environment variables.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
DB_PATH = os.environ.get("DB_PATH", "richflow_crm.sqlite3").strip() or "richflow_crm.sqlite3"

_missing = [key for key, value in (("BOT_TOKEN", BOT_TOKEN), ("OWNER_ID", OWNER_ID_RAW)) if not value]
if _missing:
    raise RuntimeError("Не заданы переменные окружения: " + ", ".join(_missing))
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("OWNER_ID должен быть числовым Telegram ID.") from exc
if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID должен быть положительным числом.")

PAGE_SIZE = 8
SEARCH_CACHE = {}
STATUSES = ["Новый", "В работе", "Ожидает ответа", "Заинтересован", "Успешно", "Отказ", "Архив"]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the four-column schema and safely migrate the previous schema."""
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'"
        ).fetchone()
        if not exists:
            conn.execute("""
                CREATE TABLE leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username_tg TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Новый',
                    created_at TEXT NOT NULL
                )
            """)
            return

        columns = [row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()]
        if columns == ["id", "username_tg", "status", "created_at"]:
            return

        # Preserve IDs, Telegram usernames, statuses and creation timestamps.
        old_cols = set(columns)
        username_expr = "telegram" if "telegram" in old_cols else ("username_tg" if "username_tg" in old_cols else "''")
        status_expr = "status" if "status" in old_cols else "'Новый'"
        created_expr = "created_at" if "created_at" in old_cols else "CURRENT_TIMESTAMP"
        conn.execute("ALTER TABLE leads RENAME TO leads_old_migration")
        conn.execute("""
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username_tg TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Новый',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(f"""
            INSERT INTO leads (id, username_tg, status, created_at)
            SELECT id, COALESCE({username_expr}, ''), COALESCE({status_expr}, 'Новый'),
                   COALESCE({created_expr}, CURRENT_TIMESTAMP)
            FROM leads_old_migration
        """)
        conn.execute("DROP TABLE leads_old_migration")


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все лиды", callback_data="list:0"),
         InlineKeyboardButton(text="➕ Добавить лида", callback_data="add")],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="search"),
         InlineKeyboardButton(text="🏷 По статусам", callback_data="statuses")],
        [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="export")],
    ])


def lead_kb(lead_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить username", callback_data=f"edit:{lead_id}"),
         InlineKeyboardButton(text="🔄 Статус", callback_data=f"setstatus:{lead_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{lead_id}"),
         InlineKeyboardButton(text="⬅️ К списку", callback_data="list:0")],
    ])


def get_lead(lead_id: int):
    with db() as conn:
        return conn.execute("SELECT id, username_tg, status, created_at FROM leads WHERE id=?", (lead_id,)).fetchone()


def lead_text(row):
    return (
        f"<b>Лид #{row['id']}</b>\n"
        f"Telegram: {escape(row['username_tg'] or '—')}\n"
        f"Статус: <b>{escape(row['status'])}</b>\n"
        f"Создан: {escape(row['created_at'])}"
    )


def list_leads(page=0, status=None, query=None):
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if query:
        where.append("username_tg LIKE ?")
        params.append(f"%{query}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads" + clause, params).fetchone()[0]
        rows = conn.execute(
            "SELECT id, username_tg, status, created_at FROM leads" + clause +
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, page * PAGE_SIZE],
        ).fetchall()
    return rows, total


def list_kb(rows, page, total, prefix="list"):
    buttons = []
    for row in rows:
        label = f"#{row['id']} @{row['username_tg']} · {row['status']}" if row['username_tg'] else f"#{row['id']} · {row['status']}"
        buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"lead:{row['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


class AddLead(StatesGroup):
    username_tg = State()


class EditLead(StatesGroup):
    username_tg = State()


class SearchLead(StatesGroup):
    query = State()


async def owner_message(message: Message) -> bool:
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("⛔ Доступ закрыт.")
        return False
    return True


async def owner_callback(call: CallbackQuery) -> bool:
    if not is_owner(call.from_user.id):
        await call.answer("Доступ закрыт", show_alert=True)
        return False
    return True


def clean_username(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("/")[0].split("?")[0]
    elif value.startswith("http://t.me/"):
        value = value.removeprefix("http://t.me/").split("/")[0].split("?")[0]
    return value.lstrip("@").strip()


async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    @dp.message(Command("menu"))
    async def cmd_start(message: Message):
        if await owner_message(message):
            await message.answer("Добро пожаловать в <b>RICHFLOW CRM</b>.\nВыбери действие:", reply_markup=main_kb())

    @dp.message(Command("id"))
    async def cmd_id(message: Message):
        await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")

    @dp.callback_query(F.data == "menu")
    async def menu(call: CallbackQuery):
        if await owner_callback(call):
            await call.message.edit_text("Главное меню RICHFLOW CRM:", reply_markup=main_kb())
            await call.answer()

    @dp.callback_query(F.data == "add")
    async def add_start(call: CallbackQuery, state: FSMContext):
        if not await owner_callback(call):
            return
        await state.clear()
        await state.set_state(AddLead.username_tg)
        await call.message.answer("➕ Введи Telegram username лида (например, @username или ссылку t.me/username):")
        await call.answer()

    @dp.message(AddLead.username_tg)
    async def add_username(message: Message, state: FSMContext):
        if not await owner_message(message):
            return
        if not message.text:
            await message.answer("Отправь username текстом, например @username.")
            return
        username = clean_username(message.text)
        if not username:
            await message.answer("Username пустой. Введи @username или отправь '-' если username неизвестен.")
            return
        if username == "-":
            username = ""
        created_at = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")
        with db() as conn:
            cursor = conn.execute(
                "INSERT INTO leads (username_tg, status, created_at) VALUES (?, ?, ?)",
                (username, "Новый", created_at),
            )
            lead_id = cursor.lastrowid
        await state.clear()
        row = get_lead(lead_id)
        await message.answer("✅ Лид добавлен.\n\n" + lead_text(row), reply_markup=lead_kb(lead_id))

    @dp.callback_query(F.data.startswith("list:"))
    async def show_list(call: CallbackQuery):
        if not await owner_callback(call):
            return
        page = int(call.data.split(":", 1)[1])
        rows, total = list_leads(page)
        if not rows:
            await call.message.edit_text("Пока лидов нет. Нажми «Добавить лида».", reply_markup=main_kb())
        else:
            await call.message.edit_text(f"<b>Все лиды</b> · {total} всего · страница {page+1}", reply_markup=list_kb(rows, page, total))
        await call.answer()

    @dp.callback_query(F.data.startswith("lead:"))
    async def show_lead(call: CallbackQuery):
        if not await owner_callback(call):
            return
        lead_id = int(call.data.split(":", 1)[1])
        row = get_lead(lead_id)
        if row:
            await call.message.edit_text(lead_text(row), reply_markup=lead_kb(lead_id))
        else:
            await call.message.edit_text("Лид не найден.", reply_markup=main_kb())
        await call.answer()

    @dp.callback_query(F.data.startswith("edit:"))
    async def edit_start(call: CallbackQuery, state: FSMContext):
        if not await owner_callback(call):
            return
        lead_id = int(call.data.split(":", 1)[1])
        if not get_lead(lead_id):
            await call.answer("Лид не найден", show_alert=True)
            return
        await state.clear()
        await state.update_data(edit_id=lead_id)
        await state.set_state(EditLead.username_tg)
        await call.message.answer("Введи новый Telegram username (или '-' чтобы очистить):")
        await call.answer()

    @dp.message(EditLead.username_tg)
    async def edit_username(message: Message, state: FSMContext):
        if not await owner_message(message):
            return
        if not message.text:
            await message.answer("Отправь username текстом.")
            return
        data = await state.get_data()
        username = clean_username(message.text)
        if username == "-":
            username = ""
        with db() as conn:
            conn.execute("UPDATE leads SET username_tg=? WHERE id=?", (username, data["edit_id"]))
        lead_id = data["edit_id"]
        await state.clear()
        await message.answer("✅ Username обновлён.\n\n" + lead_text(get_lead(lead_id)), reply_markup=lead_kb(lead_id))

    @dp.callback_query(F.data.startswith("setstatus:"))
    async def status_menu(call: CallbackQuery):
        if not await owner_callback(call):
            return
        lead_id = int(call.data.split(":", 1)[1])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=status, callback_data=f"status:{lead_id}:{index}")]
            for index, status in enumerate(STATUSES)
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"lead:{lead_id}")]])
        await call.message.edit_text("Выбери новый статус:", reply_markup=keyboard)
        await call.answer()

    @dp.callback_query(F.data.startswith("status:"))
    async def set_status(call: CallbackQuery):
        if not await owner_callback(call):
            return
        _, lead_id_raw, index_raw = call.data.split(":")
        lead_id, index = int(lead_id_raw), int(index_raw)
        if index < 0 or index >= len(STATUSES):
            await call.answer("Неизвестный статус", show_alert=True)
            return
        with db() as conn:
            conn.execute("UPDATE leads SET status=? WHERE id=?", (STATUSES[index], lead_id))
        row = get_lead(lead_id)
        if row:
            await call.message.edit_text("✅ Статус обновлён.\n\n" + lead_text(row), reply_markup=lead_kb(lead_id))
        await call.answer()

    @dp.callback_query(F.data.startswith("delete:"))
    async def delete_confirm(call: CallbackQuery):
        if not await owner_callback(call):
            return
        lead_id = int(call.data.split(":", 1)[1])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, удалить", callback_data=f"delok:{lead_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"lead:{lead_id}"),
        ]])
        await call.message.edit_text(f"Точно удалить лида #{lead_id}?", reply_markup=keyboard)
        await call.answer()

    @dp.callback_query(F.data.startswith("delok:"))
    async def delete_lead(call: CallbackQuery):
        if not await owner_callback(call):
            return
        lead_id = int(call.data.split(":", 1)[1])
        with db() as conn:
            conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        await call.message.edit_text("🗑 Лид удалён.", reply_markup=main_kb())
        await call.answer()

    @dp.callback_query(F.data == "statuses")
    async def statuses(call: CallbackQuery):
        if not await owner_callback(call):
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=status, callback_data=f"filter:{index}")]
            for index, status in enumerate(STATUSES)
        ] + [[InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]])
        await call.message.edit_text("Выбери статус:", reply_markup=keyboard)
        await call.answer()

    @dp.callback_query(F.data.startswith("filter:"))
    async def filter_status(call: CallbackQuery):
        if not await owner_callback(call):
            return
        index = int(call.data.split(":", 1)[1])
        status = STATUSES[index]
        rows, total = list_leads(0, status=status)
        await call.message.edit_text(
            f"<b>Статус: {escape(status)}</b> · {total} лидов",
            reply_markup=list_kb(rows, 0, total, prefix=f"filtered{index}") if rows else main_kb(),
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("filtered"))
    async def filtered_page(call: CallbackQuery):
        if not await owner_callback(call):
            return
        status_index, page_raw = call.data.replace("filtered", "", 1).split(":")
        status_index, page = int(status_index), int(page_raw)
        status = STATUSES[status_index]
        rows, total = list_leads(page, status=status)
        await call.message.edit_text(
            f"<b>Статус: {escape(status)}</b> · {total} лидов",
            reply_markup=list_kb(rows, page, total, prefix=f"filtered{status_index}"),
        )
        await call.answer()

    @dp.callback_query(F.data == "search")
    async def search_start(call: CallbackQuery, state: FSMContext):
        if not await owner_callback(call):
            return
        await state.clear()
        await state.set_state(SearchLead.query)
        await call.message.answer("Введи Telegram username для поиска:")
        await call.answer()

    @dp.message(SearchLead.query)
    async def search_results(message: Message, state: FSMContext):
        if not await owner_message(message):
            return
        query = clean_username(message.text or "")
        await state.clear()
        SEARCH_CACHE[message.from_user.id] = query
        rows, total = list_leads(0, query=query)
        if rows:
            await message.answer(f"Результаты поиска: {total}", reply_markup=list_kb(rows, 0, total, prefix="searchpage"))
        else:
            await message.answer("Ничего не найдено.", reply_markup=main_kb())

    @dp.callback_query(F.data.startswith("searchpage:"))
    async def search_page(call: CallbackQuery):
        if not await owner_callback(call):
            return
        query = SEARCH_CACHE.get(call.from_user.id)
        if query is None:
            await call.answer("Повтори поиск, пожалуйста.", show_alert=True)
            return
        page = int(call.data.split(":", 1)[1])
        rows, total = list_leads(page, query=query)
        await call.message.edit_text(
            f"<b>Результаты поиска: {escape(query)}</b> · {total}",
            reply_markup=list_kb(rows, page, total, prefix="searchpage"),
        )
        await call.answer()

    @dp.callback_query(F.data == "export")
    async def export_csv(call: CallbackQuery):
        if not await owner_callback(call):
            return
        with db() as conn:
            rows = conn.execute("SELECT id, username_tg, status, created_at FROM leads ORDER BY id").fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "username_tg", "status", "created_at"])
        for row in rows:
            writer.writerow([row["id"], row["username_tg"], row["status"], row["created_at"]])
        file = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="richflow_leads.csv")
        await call.message.answer_document(file, caption=f"Экспорт RICHFLOW CRM · {len(rows)} лидов")
        await call.answer()

    @dp.message()
    async def fallback(message: Message):
        if await owner_message(message):
            await message.answer("Используй меню бота: /menu", reply_markup=main_kb())

    print(f"[RICHFLOW] Starting polling; PID={os.getpid()}; DB_PATH={DB_PATH}", flush=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
