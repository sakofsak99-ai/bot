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
    BufferedInputFile
)
# Configuration is supplied by the hosting platform environment variables.
# No .env file or python-dotenv is used.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
DB_PATH = os.environ.get("DB_PATH", "richflow_crm.sqlite3").strip() or "richflow_crm.sqlite3"

_missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("OWNER_ID", OWNER_ID_RAW)) if not value]
if _missing:
    raise RuntimeError(
        "Missing required environment variable(s): " + ", ".join(_missing)
        + ". Add them in the Bothost environment variables settings."
    )
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("OWNER_ID must be a numeric Telegram user ID.") from exc
if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID must be a positive numeric Telegram user ID.")
PAGE_SIZE = 5
SEARCH_CACHE = {}

STATUSES = [
    "Новый", "В работе", "Ожидает ответа", "Заинтересован",
    "На оформлении", "Успешно", "Отказ", "Не выходит на связь", "Архив"
]
FIELDS = [
    ("name", "Имя"),
    ("telegram", "Telegram"),
    ("phone", "Телефон"),
    ("has_ip", "ИП"),
    ("city", "Город"),
    ("source", "Источник"),
    ("status", "Статус"),
    ("bank", "Банк"),
    ("last_contact", "Последний контакт"),
    ("next_contact", "Следующий контакт"),
    ("comment", "Комментарий"),
]
FIELD_MAP = dict(FIELDS)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            telegram TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            has_ip TEXT DEFAULT '',
            city TEXT DEFAULT '',
            source TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Новый',
            bank TEXT DEFAULT '',
            last_contact TEXT DEFAULT '',
            next_contact TEXT DEFAULT '',
            comment TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """)


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все лиды", callback_data="list:0"),
         InlineKeyboardButton(text="➕ Добавить лида", callback_data="add")],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="search"),
         InlineKeyboardButton(text="🏷 По статусам", callback_data="statuses")],
        [InlineKeyboardButton(text="⏰ Контакты на сегодня", callback_data="today"),
         InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="export")],
    ])


def lead_keyboard(lead_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{lead_id}"),
         InlineKeyboardButton(text="🔄 Статус", callback_data=f"setstatus:{lead_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{lead_id}"),
         InlineKeyboardButton(text="⬅️ К списку", callback_data="list:0")],
    ])


def get_lead(lead_id):
    with db() as conn:
        return conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def lead_text(row):
    def val(k):
        return escape(row[k] or "—")
    return (
        f"<b>Лид #{row['id']} · {val('name')}</b>\n"
        f"Статус: <b>{val('status')}</b>\n"
        f"Telegram: {val('telegram')}\n"
        f"Телефон: {val('phone')}\n"
        f"ИП: {val('has_ip')}\n"
        f"Город: {val('city')}\n"
        f"Источник: {val('source')}\n"
        f"Банк: {val('bank')}\n"
        f"Последний контакт: {val('last_contact')}\n"
        f"Следующий контакт: {val('next_contact')}\n"
        f"Комментарий: {val('comment')}"
    )


def list_leads(page=0, status=None, search=None):
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if search:
        where.append("(name LIKE ? OR telegram LIKE ? OR phone LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads" + clause, params).fetchone()[0]
        rows = conn.execute(
            "SELECT id,name,status,city,next_contact FROM leads" + clause +
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, page * PAGE_SIZE]
        ).fetchall()
    return rows, total


def list_kb(rows, page, total, prefix="list"):
    buttons = []
    for r in rows:
        label = f"#{r['id']} {r['name']} · {r['status']}"
        if r["city"]:
            label += f" · {r['city']}"
        buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"lead:{r['id']}")])
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
    name = State()
    telegram = State()
    phone = State()
    has_ip = State()
    city = State()
    source = State()
    bank = State()
    last_contact = State()
    next_contact = State()
    comment = State()


class EditLead(StatesGroup):
    value = State()


class SearchLead(StatesGroup):
    query = State()


async def ask_next(message: Message, state: FSMContext, field_index: int):
    prompts = [
        ("name", "Имя лида (обязательно):"),
        ("telegram", "Telegram username или ссылка (можно «-»):"),
        ("phone", "Телефон (или «-»):"),
        ("has_ip", "Есть ИП? Напиши «Да», «Нет» или «-»:"),
        ("city", "Город (или «-»):"),
        ("source", "Источник заявки (или «-»):"),
        ("bank", "Банк / предложение (или «-»):"),
        ("last_contact", "Дата последнего контакта (например, 22.11.2026 или «-»):"),
        ("next_contact", "Дата следующего контакта (например, 24.11.2026 или «-»):"),
        ("comment", "Комментарий (или «-»):"),
    ]
    key, prompt = prompts[field_index]
    await state.set_state(getattr(AddLead, key).state)
    await message.answer(prompt)


async def add_value(message: Message, state: FSMContext, key: str, next_index: int):
    value = message.text.strip()
    if value == "-":
        value = ""
    await state.update_data(**{key: value})
    if next_index < 10:
        await ask_next(message, state, next_index)
    else:
        data = await state.get_data()
        with db() as conn:
            cur = conn.execute("""
                INSERT INTO leads
                (name,telegram,phone,has_ip,city,source,bank,last_contact,next_contact,comment,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data.get("name", ""), data.get("telegram", ""), data.get("phone", ""),
                data.get("has_ip", ""), data.get("city", ""), data.get("source", ""),
                data.get("bank", ""), data.get("last_contact", ""), data.get("next_contact", ""),
                data.get("comment", ""), "Новый", datetime.now().strftime("%d.%m.%Y %H:%M")
            ))
            lead_id = cur.lastrowid
        await state.clear()
        row = get_lead(lead_id)
        await message.answer("✅ Лид добавлен.\n\n" + lead_text(row), reply_markup=lead_keyboard(lead_id))


async def start_add(message: Message, state: FSMContext):
    await state.clear()
    await ask_next(message, state, 0)


async def owner_guard_message(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Доступ закрыт.")
        return False
    return True


async def owner_guard_callback(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        await call.answer("Доступ закрыт", show_alert=True)
        return False
    return True


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в файле .env")
    if OWNER_ID == 0:
        raise RuntimeError("Не задан OWNER_ID в файле .env")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    @dp.message(Command("menu"))
    async def cmd_start(message: Message):
        if await owner_guard_message(message):
            await message.answer("Добро пожаловать в <b>RICHFLOW CRM</b>.\nВыбери действие:", reply_markup=main_kb())

    @dp.message(Command("id"))
    async def cmd_id(message: Message):
        await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")

    @dp.callback_query(F.data == "menu")
    async def menu(call: CallbackQuery):
        if await owner_guard_callback(call):
            await call.message.edit_text("Главное меню RICHFLOW CRM:", reply_markup=main_kb())
            await call.answer()

    @dp.callback_query(F.data == "add")
    async def add(call: CallbackQuery, state: FSMContext):
        if await owner_guard_callback(call):
            await call.message.answer("➕ Добавление нового лида.")
            await start_add(call.message, state)
            await call.answer()

    @dp.message(AddLead.name)
    async def a_name(m: Message, s: FSMContext): await add_value(m, s, "name", 1)
    @dp.message(AddLead.telegram)
    async def a_tg(m: Message, s: FSMContext): await add_value(m, s, "telegram", 2)
    @dp.message(AddLead.phone)
    async def a_phone(m: Message, s: FSMContext): await add_value(m, s, "phone", 3)
    @dp.message(AddLead.has_ip)
    async def a_ip(m: Message, s: FSMContext): await add_value(m, s, "has_ip", 4)
    @dp.message(AddLead.city)
    async def a_city(m: Message, s: FSMContext): await add_value(m, s, "city", 5)
    @dp.message(AddLead.source)
    async def a_source(m: Message, s: FSMContext): await add_value(m, s, "source", 6)
    @dp.message(AddLead.bank)
    async def a_bank(m: Message, s: FSMContext): await add_value(m, s, "bank", 7)
    @dp.message(AddLead.last_contact)
    async def a_last(m: Message, s: FSMContext): await add_value(m, s, "last_contact", 8)
    @dp.message(AddLead.next_contact)
    async def a_next(m: Message, s: FSMContext): await add_value(m, s, "next_contact", 9)
    @dp.message(AddLead.comment)
    async def a_comment(m: Message, s: FSMContext): await add_value(m, s, "comment", 10)

    @dp.callback_query(F.data.startswith("list:"))
    async def show_list(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        page = int(call.data.split(":")[1])
        rows, total = list_leads(page)
        if not rows:
            await call.message.edit_text("Пока лидов нет. Нажми «Добавить лида».", reply_markup=main_kb())
        else:
            text = f"<b>Все лиды</b> · {total} всего · страница {page+1}\nВыбери запись:"
            await call.message.edit_text(text, reply_markup=list_kb(rows, page, total))
        await call.answer()

    @dp.callback_query(F.data.startswith("lead:"))
    async def show_lead(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        lead_id = int(call.data.split(":")[1])
        row = get_lead(lead_id)
        if row:
            await call.message.edit_text(lead_text(row), reply_markup=lead_keyboard(lead_id))
        else:
            await call.message.edit_text("Лид не найден.", reply_markup=main_kb())
        await call.answer()

    @dp.callback_query(F.data.startswith("edit:"))
    async def edit_menu(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        lead_id = int(call.data.split(":")[1])
        row = get_lead(lead_id)
        if not row:
            await call.answer("Лид не найден", show_alert=True); return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"field:{lead_id}:{key}")]
            for key, label in FIELDS if key != "status"
        ] + [[InlineKeyboardButton(text="⬅️ К карточке", callback_data=f"lead:{lead_id}")]])
        await call.message.edit_text("Какое поле изменить?", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data.startswith("field:"))
    async def edit_field(call: CallbackQuery, state: FSMContext):
        if not await owner_guard_callback(call): return
        _, lid, key = call.data.split(":")
        await state.update_data(edit_id=int(lid), edit_key=key)
        await state.set_state(EditLead.value)
        await call.message.answer(f"Введи новое значение для поля «{FIELD_MAP[key]}». Для очистки отправь дефис -")
        await call.answer()

    @dp.message(EditLead.value)
    async def save_field(message: Message, state: FSMContext):
        if not await owner_guard_message(message): return
        data = await state.get_data()
        value = message.text.strip()
        if value == "-": value = ""
        with db() as conn:
            conn.execute(f"UPDATE leads SET {data['edit_key']}=? WHERE id=?", (value, data["edit_id"]))
        lead_id = data["edit_id"]
        await state.clear()
        row = get_lead(lead_id)
        await message.answer("✅ Изменения сохранены.\n\n" + lead_text(row), reply_markup=lead_keyboard(lead_id))

    @dp.callback_query(F.data.startswith("setstatus:"))
    async def status_menu(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        lid = int(call.data.split(":")[1])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s, callback_data=f"status:{lid}:{i}")]
            for i, s in enumerate(STATUSES)
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"lead:{lid}")]])
        await call.message.edit_text("Выбери новый статус:", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data.startswith("status:"))
    async def set_status(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        _, lid, idx = call.data.split(":")
        status = STATUSES[int(idx)]
        with db() as conn:
            conn.execute("UPDATE leads SET status=? WHERE id=?", (status, int(lid)))
        row = get_lead(int(lid))
        await call.message.edit_text("✅ Статус обновлён.\n\n" + lead_text(row), reply_markup=lead_keyboard(int(lid)))
        await call.answer()

    @dp.callback_query(F.data.startswith("delete:"))
    async def delete_confirm(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        lid = int(call.data.split(":")[1])
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, удалить", callback_data=f"delok:{lid}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"lead:{lid}")
        ]])
        await call.message.edit_text(f"Точно удалить лида #{lid}? Это действие нельзя отменить.", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data.startswith("delok:"))
    async def delete_lead(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        lid = int(call.data.split(":")[1])
        with db() as conn:
            conn.execute("DELETE FROM leads WHERE id=?", (lid,))
        await call.message.edit_text("🗑 Лид удалён.", reply_markup=main_kb())
        await call.answer()

    @dp.callback_query(F.data == "statuses")
    async def statuses(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s, callback_data=f"filter:{i}")]
            for i, s in enumerate(STATUSES)
        ] + [[InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]])
        await call.message.edit_text("Выбери статус для фильтрации:", reply_markup=kb)
        await call.answer()

    @dp.callback_query(F.data.startswith("filter:"))
    async def filter_status(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        status = STATUSES[int(call.data.split(":")[1])]
        rows, total = list_leads(0, status=status)
        await call.message.edit_text(
            f"<b>Статус: {escape(status)}</b> · {total} лидов",
            reply_markup=list_kb(rows, 0, total, prefix=f"filtered{int(call.data.split(':')[1])}") if rows else main_kb()
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("filtered"))
    async def filtered_page(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        status_index, page = call.data.replace("filtered", "").split(":")
        status = STATUSES[int(status_index)]
        rows, total = list_leads(int(page), status=status)
        await call.message.edit_text(
            f"<b>Статус: {escape(status)}</b> · {total} лидов",
            reply_markup=list_kb(rows, int(page), total, prefix=f"filtered{status_index}")
        )
        await call.answer()

    @dp.callback_query(F.data == "search")
    async def search_start(call: CallbackQuery, state: FSMContext):
        if not await owner_guard_callback(call): return
        await state.set_state(SearchLead.query)
        await call.message.answer("Введи имя, Telegram username или телефон для поиска:")
        await call.answer()

    @dp.message(SearchLead.query)
    async def search_results(message: Message, state: FSMContext):
        if not await owner_guard_message(message): return
        query = message.text.strip()
        await state.clear()
        SEARCH_CACHE[message.from_user.id] = query
        rows, total = list_leads(0, search=query)
        if rows:
            await message.answer(f"Результаты поиска: {total}", reply_markup=list_kb(rows, 0, total, prefix="searchpage"))
        else:
            await message.answer("Ничего не найдено.", reply_markup=main_kb())

    @dp.callback_query(F.data.startswith("searchpage:"))
    async def search_page(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        query = SEARCH_CACHE.get(call.from_user.id)
        if not query:
            await call.answer("Повтори поиск, пожалуйста.", show_alert=True)
            return
        page = int(call.data.split(":")[1])
        rows, total = list_leads(page, search=query)
        await call.message.edit_text(
            f"<b>Результаты поиска: {escape(query)}</b> · {total}",
            reply_markup=list_kb(rows, page, total, prefix="searchpage")
        )
        await call.answer()

    @dp.callback_query(F.data == "today")
    async def today_contacts(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        today = datetime.now().strftime("%d.%m.%Y")
        with db() as conn:
            rows = conn.execute(
                "SELECT id,name,status,city,next_contact FROM leads WHERE next_contact=? ORDER BY id DESC",
                (today,)
            ).fetchall()
        if rows:
            await call.message.edit_text(
                f"<b>Контакты на сегодня · {today}</b>",
                reply_markup=list_kb(rows, 0, len(rows))
            )
        else:
            await call.message.edit_text(f"На сегодня ({today}) контактов не запланировано.", reply_markup=main_kb())
        await call.answer()

    @dp.callback_query(F.data == "export")
    async def export_csv(call: CallbackQuery):
        if not await owner_guard_callback(call): return
        with db() as conn:
            rows = conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        headers = ["ID"] + [label for _, label in FIELDS] + ["Создан"]
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r["id"]] + [r[k] for k, _ in FIELDS] + [r["created_at"]])
        data = output.getvalue().encode("utf-8-sig")
        file = BufferedInputFile(data, filename="richflow_leads.csv")
        await call.message.answer_document(file, caption=f"Экспорт RICHFLOW CRM · {len(rows)} лидов")
        await call.answer()

    @dp.message()
    async def fallback(message: Message):
        if await owner_guard_message(message):
            await message.answer("Используй меню бота: /menu", reply_markup=main_kb())

    print(f"[RICHFLOW] Starting polling; PID={os.getpid()}; OWNER_ID configured; DB_PATH={DB_PATH}", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
