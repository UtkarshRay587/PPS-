"""
Premium Telegram Digital Store Bot
==================================
A complete, single-file Telegram digital marketplace bot.

Requirements:
    python-telegram-bot >= 22.0
    Python 3.12+

Just edit the CONFIG section below and run:
    python bot.py
"""

# ═══════════════════════════════════════════════════════════════════════
#                          CONFIG  (EDIT THIS)
# ═══════════════════════════════════════════════════════════════════════

BOT_TOKEN         = "8560871955:AAGu8-iv6tlXWxd4I2mzS1SRyV26AQShtJg"                       # Bot token from @BotFather
OWNER_ID          = 8260250479                # Your Telegram numeric user ID
STORE_NAME        = "𝙋𝙧𝙤𝙛𝙚𝙨𝙨𝙤𝙧 𝙋𝙧𝙤 𝙎𝙝𝙤𝙥"  # Your store name
STORE_USERNAME    = "ppshop_updates"     # Store channel username (no @)
SUPPORT_USERNAME  = "prosupport_robot"           # Support username (no @)
UPI_ID            = "utkarshvikas@fam"           # Your UPI ID
QR_CODE           = "utkarshvikasqr.png"                       # Telegram File ID OR local path to QR image
LOG_CHANNEL_ID    = -1004486046447                        # Optional log channel ID (0 to disable)

CURRENCY          = "₹"                      # Currency symbol
DB_PATH           = "store.db"               # SQLite database path

CATEGORIES = [
    "PFP",
    "CC",
    "CC Presets",
    "VFP Presets",
    "Transition Presets",
    "Outro Presets",
]

# ═══════════════════════════════════════════════════════════════════════
#                              IMPORTS
# ═══════════════════════════════════════════════════════════════════════

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("store-bot")


# ═══════════════════════════════════════════════════════════════════════
#                              DATABASE
# ═══════════════════════════════════════════════════════════════════════

class DB:
    """Thin SQLite wrapper. All calls are synchronous but fast enough."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ─── schema ─────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                balance      REAL DEFAULT 0,
                banned       INTEGER DEFAULT 0,
                joined_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS products (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                description  TEXT,
                category     TEXT NOT NULL,
                price        REAL NOT NULL,
                preview_type TEXT,            -- 'photo' or 'video' or NULL
                preview_id   TEXT,            -- Telegram file_id
                files_json   TEXT DEFAULT '[]',   -- JSON list of {type,file_id,name}
                created_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id     TEXT PRIMARY KEY,
                user_id      INTEGER,
                product_id   INTEGER,
                price        REAL,
                method       TEXT,       -- 'wallet' or 'upi'
                status       TEXT,       -- 'pending','completed','rejected'
                screenshot   TEXT,       -- file_id if UPI
                created_at   TEXT,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS wallet_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER,
                amount       REAL,
                type         TEXT,       -- 'credit' or 'debit'
                note         TEXT,
                created_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS wallet_requests (
                req_id       TEXT PRIMARY KEY,
                user_id      INTEGER,
                amount       REAL,
                screenshot   TEXT,
                status       TEXT,   -- 'pending','approved','rejected'
                created_at   TEXT,
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS redeem_codes (
                code         TEXT PRIMARY KEY,
                amount       REAL,
                expiry       TEXT,           -- ISO or NULL
                usage_limit  INTEGER,
                used_count   INTEGER DEFAULT 0,
                used_by      TEXT DEFAULT '[]'   -- JSON list of user_ids
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.conn.commit()

    # ─── users ──────────────────────────────────────────────────────
    def upsert_user(self, uid: int, username: str, first_name: str) -> bool:
        """Return True if a new user was created."""
        row = self.conn.execute(
            "SELECT user_id FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (username, first_name, uid),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO users(user_id,username,first_name,balance,joined_at) "
            "VALUES(?,?,?,0,?)",
            (uid, username, first_name, now_iso()),
        )
        self.conn.commit()
        return True

    def get_user(self, uid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE user_id=?", (uid,)
        ).fetchone()

    def find_user(self, needle: str) -> Optional[sqlite3.Row]:
        needle = needle.strip().lstrip("@")
        if needle.isdigit():
            return self.get_user(int(needle))
        return self.conn.execute(
            "SELECT * FROM users WHERE lower(username)=lower(?)", (needle,)
        ).fetchone()

    def all_users(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM users").fetchall())

    def users_joined_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE substr(joined_at,1,10)=?", (today,)
        ).fetchone()[0]

    # ─── balance ────────────────────────────────────────────────────
    def get_balance(self, uid: int) -> float:
        r = self.conn.execute(
            "SELECT balance FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        return float(r["balance"]) if r else 0.0

    def add_balance(self, uid: int, amount: float, note: str) -> None:
        self.conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, uid)
        )
        self._add_history(uid, amount, "credit" if amount >= 0 else "debit", note)
        self.conn.commit()

    def set_balance(self, uid: int, amount: float, note: str) -> None:
        self.conn.execute(
            "UPDATE users SET balance=? WHERE user_id=?", (amount, uid)
        )
        self._add_history(uid, amount, "credit", note)
        self.conn.commit()

    def _add_history(self, uid: int, amount: float, ttype: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO wallet_history(user_id,amount,type,note,created_at) "
            "VALUES(?,?,?,?,?)",
            (uid, amount, ttype, note, now_iso()),
        )

    def wallet_history(self, uid: int, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM wallet_history WHERE user_id=? "
                "ORDER BY id DESC LIMIT ?",
                (uid, limit),
            ).fetchall()
        )

    # ─── products ───────────────────────────────────────────────────
    def add_product(self, **kw: Any) -> int:
        cur = self.conn.execute(
            "INSERT INTO products(name,description,category,price,"
            "preview_type,preview_id,files_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                kw["name"], kw["description"], kw["category"], kw["price"],
                kw.get("preview_type"), kw.get("preview_id"),
                kw.get("files_json", "[]"), now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_product(self, pid: int, field: str, value: Any) -> None:
        allowed = {
            "name", "description", "category", "price",
            "preview_type", "preview_id", "files_json",
        }
        if field not in allowed:
            return
        self.conn.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))
        self.conn.commit()

    def delete_product(self, pid: int) -> None:
        self.conn.execute("DELETE FROM products WHERE id=?", (pid,))
        self.conn.commit()

    def get_product(self, pid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM products WHERE id=?", (pid,)
        ).fetchone()

    def products_by_category(self, category: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM products WHERE category=? ORDER BY id DESC",
                (category,),
            ).fetchall()
        )

    def all_products(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM products ORDER BY id DESC"))

    # ─── orders ─────────────────────────────────────────────────────
    def add_order(self, **kw: Any) -> None:
        self.conn.execute(
            "INSERT INTO orders(order_id,user_id,product_id,price,method,"
            "status,screenshot,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                kw["order_id"], kw["user_id"], kw["product_id"], kw["price"],
                kw["method"], kw["status"], kw.get("screenshot"),
                now_iso(), kw.get("completed_at"),
            ),
        )
        self.conn.commit()

    def update_order(self, order_id: str, **fields: Any) -> None:
        keys = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE orders SET {keys} WHERE order_id=?",
            (*fields.values(), order_id),
        )
        self.conn.commit()

    def get_order(self, order_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()

    def user_orders(self, uid: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC",
                (uid,),
            ).fetchall()
        )

    def user_purchased_products(self, uid: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT DISTINCT p.* FROM orders o "
                "JOIN products p ON p.id=o.product_id "
                "WHERE o.user_id=? AND o.status='completed' "
                "ORDER BY o.created_at DESC",
                (uid,),
            ).fetchall()
        )

    def all_orders(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    # ─── wallet requests ────────────────────────────────────────────
    def add_wallet_request(self, req_id: str, uid: int, amount: float,
                           screenshot: str) -> None:
        self.conn.execute(
            "INSERT INTO wallet_requests(req_id,user_id,amount,screenshot,"
            "status,created_at) VALUES(?,?,?,?,?,?)",
            (req_id, uid, amount, screenshot, "pending", now_iso()),
        )
        self.conn.commit()

    def get_wallet_request(self, req_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM wallet_requests WHERE req_id=?", (req_id,)
        ).fetchone()

    def update_wallet_request(self, req_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE wallet_requests SET status=?, processed_at=? WHERE req_id=?",
            (status, now_iso(), req_id),
        )
        self.conn.commit()

    # ─── redeem ─────────────────────────────────────────────────────
    def add_redeem_code(self, code: str, amount: float,
                        expiry: Optional[str], limit: int) -> None:
        self.conn.execute(
            "INSERT INTO redeem_codes(code,amount,expiry,usage_limit) "
            "VALUES(?,?,?,?)", (code, amount, expiry, limit),
        )
        self.conn.commit()

    def get_redeem_code(self, code: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM redeem_codes WHERE code=?", (code,)
        ).fetchone()

    def use_redeem(self, code: str, uid: int) -> None:
        import json as _j
        row = self.get_redeem_code(code)
        if not row:
            return
        used = _j.loads(row["used_by"])
        used.append(uid)
        self.conn.execute(
            "UPDATE redeem_codes SET used_count=used_count+1, used_by=? "
            "WHERE code=?", (_j.dumps(used), code),
        )
        self.conn.commit()

    def all_redeem_codes(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM redeem_codes"))

    def delete_redeem_code(self, code: str) -> None:
        self.conn.execute("DELETE FROM redeem_codes WHERE code=?", (code,))
        self.conn.commit()

    # ─── stats ──────────────────────────────────────────────────────
    def stats(self) -> dict:
        c = self.conn.execute
        return {
            "users":     c("SELECT COUNT(*) FROM users").fetchone()[0],
            "today":     self.users_joined_today(),
            "sold":      c("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0],
            "revenue":   c("SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed'").fetchone()[0],
            "pending":   c("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0],
            "completed": c("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0],
            "wreqs":     c("SELECT COUNT(*) FROM wallet_requests WHERE status='pending'").fetchone()[0],
            "orders":    c("SELECT COUNT(*) FROM orders").fetchone()[0],
        }


# ═══════════════════════════════════════════════════════════════════════
#                              HELPERS
# ═══════════════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def fmt_dt(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M")
    except Exception:
        return iso

def money(x: float) -> str:
    return f"{CURRENCY}{x:.2f}"

def gen_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10].upper()}"

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Compact keyboard builder: rows of (text, callback_data)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t, callback_data=c) for t, c in row]
        for row in rows
    ])

def esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


db = DB(DB_PATH)


# ═══════════════════════════════════════════════════════════════════════
#                              LOGGING
# ═══════════════════════════════════════════════════════════════════════

async def log_channel(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("log_channel failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════
#                              SEND HELPERS
# ═══════════════════════════════════════════════════════════════════════

async def send_or_edit(
    update: Update,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Edit the current callback message if possible, else send new."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    tgt = update.effective_chat
    await tgt.send_message(
        text, reply_markup=markup, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def send_qr(chat, context: ContextTypes.DEFAULT_TYPE, caption: str,
                  markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Send the configured QR image (file_id, path, or fallback text)."""
    if QR_CODE and os.path.exists(QR_CODE):
        with open(QR_CODE, "rb") as f:
            await context.bot.send_photo(
                chat.id, f, caption=caption, reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
    elif QR_CODE:
        try:
            await context.bot.send_photo(
                chat.id, QR_CODE, caption=caption, reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await chat.send_message(
                caption, reply_markup=markup, parse_mode=ParseMode.HTML,
            )
    else:
        await chat.send_message(
            caption, reply_markup=markup, parse_mode=ParseMode.HTML,
        )


# ═══════════════════════════════════════════════════════════════════════
#                              HOME / MAIN MENU
# ═══════════════════════════════════════════════════════════════════════

WELCOME = (
    "✨ <b>Welcome to {store}</b> ✨\n\n"
    "Hey <b>{name}</b>! 👋\n\n"
    "🛒 Premium digital assets at your fingertips.\n"
    "💎 PFPs • CCs • Presets • VFPs • Transitions • Outros\n\n"
    "Choose an option below to get started:"
)

def home_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [("🛍 Shop", "home:shop"), ("💰 Wallet", "home:wallet")],
        [("📦 Purchased", "home:purchased"), ("🛒 My Orders", "home:orders")],
        [("🎁 Redeem", "home:redeem"), ("❓ Support", "home:support")],
        [("📢 Updates", "home:updates")],
    ]
    if is_owner(uid):
        rows.append([("⚙️ Owner Panel", "owner:home")])
    return kb(rows)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    u = update.effective_user
    created = db.upsert_user(u.id, u.username or "", u.first_name or "")
    if created:
        await log_channel(
            context,
            f"🆕 <b>New User</b>\n"
            f"Name: {esc(u.first_name or '')}\n"
            f"Username: @{esc(u.username or '—')}\n"
            f"ID: <code>{u.id}</code>",
        )
    text = WELCOME.format(store=esc(STORE_NAME), name=esc(u.first_name or "friend"))
    await update.message.reply_html(text, reply_markup=home_kb(u.id))

async def cb_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("state", None)
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    text = WELCOME.format(store=esc(STORE_NAME), name=esc(u.first_name or "friend"))
    await send_or_edit(update, text, home_kb(u.id))


# ═══════════════════════════════════════════════════════════════════════
#                              SHOP
# ═══════════════════════════════════════════════════════════════════════

async def cb_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    rows = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        for cat in CATEGORIES[i:i + 2]:
            count = len(db.products_by_category(cat))
            row.append((f"{cat} ({count})", f"cat:{cat}"))
        rows.append(row)
    rows.append([("🔙 Back", "home:main")])
    await send_or_edit(
        update,
        "🛍 <b>Shop Categories</b>\n\nBrowse our premium collections:",
        kb(rows),
    )

async def cb_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    cat = q.data.split(":", 1)[1]
    products = db.products_by_category(cat)
    if not products:
        await send_or_edit(
            update,
            f"📭 <b>{esc(cat)}</b>\n\nNo products available in this category yet.",
            kb([[("🔙 Back", "home:shop")]]),
        )
        return
    rows = [[(f"{p['name']} — {money(p['price'])}", f"prod:{p['id']}")]
            for p in products]
    rows.append([("🔙 Back", "home:shop")])
    await send_or_edit(
        update, f"📂 <b>{esc(cat)}</b>\n\nSelect a product to view details:",
        kb(rows),
    )

async def cb_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    p = db.get_product(pid)
    if not p:
        await q.answer("Product not found", show_alert=True)
        return
    caption = (
        f"💎 <b>{esc(p['name'])}</b>\n\n"
        f"📂 <b>Category:</b> {esc(p['category'])}\n"
        f"💰 <b>Price:</b> {money(p['price'])}\n\n"
        f"📝 <b>Description:</b>\n{esc(p['description'] or '—')}"
    )
    markup = kb([
        [("🛒 Buy Now", f"buy:{p['id']}")],
        [("🔙 Back", f"cat:{p['category']}")],
    ])
    # send preview if present
    try:
        if p["preview_type"] == "photo" and p["preview_id"]:
            await context.bot.send_photo(
                q.message.chat_id, p["preview_id"],
                caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML,
            )
        elif p["preview_type"] == "video" and p["preview_id"]:
            await context.bot.send_video(
                q.message.chat_id, p["preview_id"],
                caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML,
            )
        else:
            await send_or_edit(update, caption, markup)
    except Exception:
        await send_or_edit(update, caption, markup)


# ═══════════════════════════════════════════════════════════════════════
#                              BUY FLOW
# ═══════════════════════════════════════════════════════════════════════

async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    p = db.get_product(pid)
    if not p:
        await q.answer("Product no longer available", show_alert=True)
        return
    balance = db.get_balance(q.from_user.id)
    text = (
        f"🛒 <b>Confirm Purchase</b>\n\n"
        f"💎 <b>{esc(p['name'])}</b>\n"
        f"💰 Price: {money(p['price'])}\n"
        f"👛 Your Balance: {money(balance)}\n\n"
        f"Choose payment method:"
    )
    rows = []
    if balance >= p["price"]:
        rows.append([("💳 Pay with Wallet", f"paywallet:{pid}")])
    rows.append([("💸 Pay with UPI", f"payupi:{pid}")])
    rows.append([("🔙 Back", f"prod:{pid}")])
    await q.message.reply_html(text, reply_markup=kb(rows))


async def cb_pay_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    uid = q.from_user.id
    p = db.get_product(pid)
    if not p:
        return
    bal = db.get_balance(uid)
    if bal < p["price"]:
        await q.answer("Insufficient balance!", show_alert=True)
        return
    db.add_balance(uid, -p["price"], f"Purchase: {p['name']}")
    order_id = gen_id("ORD")
    db.add_order(order_id=order_id, user_id=uid, product_id=pid,
                 price=p["price"], method="wallet", status="completed",
                 completed_at=now_iso())
    await deliver_product(context, uid, p, order_id)
    await log_channel(
        context,
        f"🛒 <b>Purchase (Wallet)</b>\n"
        f"User: <code>{uid}</code>\nProduct: {esc(p['name'])}\n"
        f"Amount: {money(p['price'])}\nOrder: <code>{order_id}</code>",
    )


async def cb_pay_upi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    p = db.get_product(pid)
    if not p:
        return
    order_id = gen_id("ORD")
    context.user_data["pending_purchase"] = {
        "order_id": order_id, "product_id": pid, "price": p["price"],
    }
    context.user_data["state"] = "await_purchase_screenshot_prompt"
    caption = (
        f"💸 <b>UPI Payment</b>\n\n"
        f"💎 Product: {esc(p['name'])}\n"
        f"💰 Amount: <b>{money(p['price'])}</b>\n"
        f"🏦 UPI ID: <code>{esc(UPI_ID)}</code>\n"
        f"🧾 Order ID: <code>{order_id}</code>\n\n"
        f"👉 Pay <b>exactly {money(p['price'])}</b> and click "
        f"<b>✅ Payment Done</b>."
    )
    markup = kb([
        [("✅ Payment Done", f"pdone:{order_id}"),
         ("❌ Cancel",       f"pcancel:{order_id}")],
    ])
    await send_qr(q.message.chat, context, caption, markup)


async def cb_pdone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data["state"] = "purchase_screenshot"
    await q.message.reply_html(
        "📸 Please upload your payment <b>screenshot</b> now.\n\n"
        "Only image files are accepted.",
        reply_markup=kb([[("❌ Cancel", "home:main")]]),
    )


async def cb_pcancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Cancelled")
    context.user_data.pop("pending_purchase", None)
    context.user_data.pop("state", None)
    await q.message.reply_html(
        "❌ Payment cancelled.", reply_markup=home_kb(q.from_user.id)
    )


async def handle_purchase_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    photo = update.message.photo[-1].file_id
    data = context.user_data.get("pending_purchase")
    if not data:
        await update.message.reply_text("Session expired. Start over with /start")
        return
    p = db.get_product(data["product_id"])
    if not p:
        await update.message.reply_text("Product no longer available.")
        return
    u = update.effective_user
    order_id = data["order_id"]
    db.add_order(order_id=order_id, user_id=u.id, product_id=p["id"],
                 price=p["price"], method="upi", status="pending",
                 screenshot=photo)
    await update.message.reply_html(
        "✅ Your payment screenshot has been received.\n\n"
        "Your payment will be verified shortly.\n"
        "After approval your product will be delivered automatically.",
        reply_markup=home_kb(u.id),
    )
    context.user_data.pop("pending_purchase", None)
    context.user_data.pop("state", None)
    # forward to owner
    caption = (
        f"🧾 <b>New Purchase Payment</b>\n\n"
        f"👤 Name: {esc(u.first_name or '')}\n"
        f"🔗 Username: @{esc(u.username or '—')}\n"
        f"🆔 User ID: <code>{u.id}</code>\n"
        f"💎 Product: {esc(p['name'])}\n"
        f"💰 Price: {money(p['price'])}\n"
        f"🧾 Order ID: <code>{order_id}</code>\n"
        f"🕐 Time: {fmt_dt(now_iso())}"
    )
    markup = kb([[("✅ Approve", f"oapprove:{order_id}"),
                  ("❌ Reject",  f"oreject:{order_id}")]])
    await context.bot.send_photo(
        OWNER_ID, photo, caption=caption,
        reply_markup=markup, parse_mode=ParseMode.HTML,
    )
    await log_channel(
        context,
        f"💳 <b>Purchase Request</b>\nUser: <code>{u.id}</code>\n"
        f"Product: {esc(p['name'])}\nOrder: <code>{order_id}</code>",
    )


async def deliver_product(
    context: ContextTypes.DEFAULT_TYPE, uid: int, p: sqlite3.Row, order_id: str,
) -> None:
    import json as _j
    await context.bot.send_message(
        uid,
        f"🎉 <b>Order Delivered!</b>\n\n"
        f"💎 {esc(p['name'])}\n"
        f"🧾 Order: <code>{order_id}</code>\n\n"
        f"Files below 👇",
        parse_mode=ParseMode.HTML,
    )
    files = _j.loads(p["files_json"] or "[]")
    if not files:
        await context.bot.send_message(
            uid, "⚠️ No files attached to this product. Contact support.",
        )
        return
    for f in files:
        try:
            ft = f.get("type")
            fid = f.get("file_id")
            name = f.get("name", "")
            if ft == "photo":
                await context.bot.send_photo(uid, fid)
            elif ft == "video":
                await context.bot.send_video(uid, fid)
            elif ft == "animation":
                await context.bot.send_animation(uid, fid)
            else:
                await context.bot.send_document(uid, fid, caption=name)
        except Exception as e:
            logger.warning("deliver file failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════
#                          OWNER ORDER APPROVAL
# ═══════════════════════════════════════════════════════════════════════

async def cb_owner_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Not authorized", show_alert=True); return
    order_id = q.data.split(":", 1)[1]
    order = db.get_order(order_id)
    if not order:
        await q.answer("Order not found", show_alert=True); return
    if order["status"] != "pending":
        await q.answer("Already processed", show_alert=True); return
    p = db.get_product(order["product_id"])
    db.update_order(order_id, status="completed", completed_at=now_iso())
    await q.answer("Approved ✅")
    try:
        await q.edit_message_caption(
            (q.message.caption or "") + "\n\n✅ <b>APPROVED</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    if p:
        await deliver_product(context, order["user_id"], p, order_id)
    await log_channel(
        context, f"✅ <b>Order Approved</b> <code>{order_id}</code>"
    )


async def cb_owner_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Not authorized", show_alert=True); return
    order_id = q.data.split(":", 1)[1]
    order = db.get_order(order_id)
    if not order or order["status"] != "pending":
        await q.answer("Already processed", show_alert=True); return
    context.user_data["state"] = "reject_order_reason"
    context.user_data["reject_order_id"] = order_id
    await q.answer()
    await q.message.reply_html(
        f"✏️ Send the rejection reason for order <code>{order_id}</code>:"
    )


# ═══════════════════════════════════════════════════════════════════════
#                              WALLET
# ═══════════════════════════════════════════════════════════════════════

async def cb_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    bal = db.get_balance(q.from_user.id)
    text = (
        f"💰 <b>Your Wallet</b>\n\n"
        f"💎 Balance: <b>{money(bal)}</b>\n\n"
        f"Manage your wallet below:"
    )
    await send_or_edit(update, text, kb([
        [("➕ Add Money", "wallet:add")],
        [("📜 Balance History", "wallet:history")],
        [("🔙 Back", "home:main")],
    ]))


async def cb_wallet_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data["state"] = "add_money_amount"
    await send_or_edit(
        update,
        "💰 <b>Add Money</b>\n\nEnter the amount you want to add:",
        kb([[("❌ Cancel", "home:wallet")]]),
    )


async def cb_wallet_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    await q.answer()
    rows = db.wallet_history(q.from_user.id)
    if not rows:
        text = "📜 <b>Balance History</b>\n\nNo transactions yet."
    else:
        lines = ["📜 <b>Balance History</b>", ""]
        for r in rows:
            sign = "➕" if r["type"] == "credit" else "➖"
            lines.append(
                f"{sign} {money(abs(r['amount']))} — {esc(r['note'])} "
                f"<i>({fmt_dt(r['created_at'])})</i>"
            )
        text = "\n".join(lines)
    await send_or_edit(update, text, kb([[("🔙 Back", "home:wallet")]]))


async def handle_add_money_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid amount. Please enter a positive number.")
        return
    context.user_data["pending_topup"] = {"amount": amount}
    context.user_data["state"] = "add_money_await_done"
    caption = (
        f"💸 <b>Add Money via UPI</b>\n\n"
        f"💰 Amount: <b>{money(amount)}</b>\n"
        f"🏦 UPI ID: <code>{esc(UPI_ID)}</code>\n\n"
        f"Pay <b>exactly {money(amount)}</b>."
    )
    markup = kb([[("✅ Payment Done", "topup:done"),
                  ("❌ Cancel", "home:wallet")]])
    await send_qr(update.effective_chat, context, caption, markup)


async def cb_topup_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data["state"] = "add_money_screenshot"
    await q.message.reply_html(
        "📸 Upload payment <b>screenshot</b> (image only).",
        reply_markup=kb([[("❌ Cancel", "home:wallet")]]),
    )


async def handle_topup_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    data = context.user_data.get("pending_topup")
    if not data:
        await update.message.reply_text("Session expired. Start over.")
        return
    photo = update.message.photo[-1].file_id
    req_id = gen_id("WR")
    u = update.effective_user
    db.add_wallet_request(req_id, u.id, data["amount"], photo)
    await update.message.reply_html(
        "✅ Screenshot received.\n\n"
        "Your request has been submitted.\n"
        "Wallet balance will be updated after verification.",
        reply_markup=home_kb(u.id),
    )
    context.user_data.pop("pending_topup", None)
    context.user_data.pop("state", None)
    caption = (
        f"💰 <b>Wallet Top-Up Request</b>\n\n"
        f"👤 Name: {esc(u.first_name or '')}\n"
        f"🔗 Username: @{esc(u.username or '—')}\n"
        f"🆔 User ID: <code>{u.id}</code>\n"
        f"💰 Amount: {money(data['amount'])}\n"
        f"🕐 Time: {fmt_dt(now_iso())}\n"
        f"🧾 Req ID: <code>{req_id}</code>"
    )
    markup = kb([[("✅ Approve", f"wapprove:{req_id}"),
                  ("❌ Reject",  f"wreject:{req_id}")]])
    await context.bot.send_photo(
        OWNER_ID, photo, caption=caption,
        reply_markup=markup, parse_mode=ParseMode.HTML,
    )
    await log_channel(
        context,
        f"💰 <b>Wallet Request</b>\nUser: <code>{u.id}</code> "
        f"Amount: {money(data['amount'])}",
    )


async def cb_wallet_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Not authorized", show_alert=True); return
    req_id = q.data.split(":", 1)[1]
    r = db.get_wallet_request(req_id)
    if not r:
        await q.answer("Not found", show_alert=True); return
    if r["status"] != "pending":
        await q.answer("Already processed", show_alert=True); return
    db.update_wallet_request(req_id, "approved")
    db.add_balance(r["user_id"], r["amount"], f"Top-up {req_id}")
    await q.answer("Approved ✅")
    try:
        await q.edit_message_caption(
            (q.message.caption or "") + "\n\n✅ <b>APPROVED</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await context.bot.send_message(
        r["user_id"],
        f"🎉 Your wallet has been credited with <b>{money(r['amount'])}</b>!\n"
        f"New balance: <b>{money(db.get_balance(r['user_id']))}</b>",
        parse_mode=ParseMode.HTML,
    )
    await log_channel(
        context, f"✅ Wallet approved for <code>{r['user_id']}</code> "
                 f"{money(r['amount'])}"
    )


async def cb_wallet_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Not authorized", show_alert=True); return
    req_id = q.data.split(":", 1)[1]
    r = db.get_wallet_request(req_id)
    if not r or r["status"] != "pending":
        await q.answer("Already processed", show_alert=True); return
    context.user_data["state"] = "reject_wallet_reason"
    context.user_data["reject_wallet_id"] = req_id
    await q.answer()
    await q.message.reply_html(
        f"✏️ Send rejection reason for wallet request <code>{req_id}</code>:"
    )


# ═══════════════════════════════════════════════════════════════════════
#                              PURCHASED & ORDERS
# ═══════════════════════════════════════════════════════════════════════

async def cb_purchased(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    products = db.user_purchased_products(q.from_user.id)
    if not products:
        await send_or_edit(update, "📦 <b>Purchased</b>\n\nNo purchases yet.",
                           kb([[("🔙 Back", "home:main")]]))
        return
    rows = [[(p["name"], f"redl:{p['id']}")] for p in products]
    rows.append([("🔙 Back", "home:main")])
    await send_or_edit(
        update,
        "📦 <b>Your Purchased Products</b>\n\nTap to re-download files:",
        kb(rows),
    )


async def cb_redownload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Delivering…")
    pid = int(q.data.split(":", 1)[1])
    p = db.get_product(pid)
    if not p:
        return
    # verify ownership
    owned = any(o["product_id"] == pid and o["status"] == "completed"
                for o in db.user_orders(q.from_user.id))
    if not owned:
        await q.answer("You don't own this product.", show_alert=True)
        return
    await deliver_product(context, q.from_user.id, p, "REDL")


async def cb_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    orders = db.user_orders(q.from_user.id)
    if not orders:
        await send_or_edit(update, "🛒 <b>My Orders</b>\n\nNo orders yet.",
                           kb([[("🔙 Back", "home:main")]])); return
    lines = ["🛒 <b>My Orders</b>", ""]
    for o in orders[:25]:
        p = db.get_product(o["product_id"])
        pname = p["name"] if p else "—"
        status_emoji = {"completed": "✅", "pending": "⏳", "rejected": "❌"}\
            .get(o["status"], "•")
        lines.append(
            f"{status_emoji} <code>{o['order_id']}</code>\n"
            f"   💎 {esc(pname)}\n"
            f"   💰 {money(o['price'])} • {esc(o['method'].upper())}\n"
            f"   📅 {fmt_dt(o['created_at'])}\n"
        )
    await send_or_edit(update, "\n".join(lines),
                       kb([[("🔙 Back", "home:main")]]))


# ═══════════════════════════════════════════════════════════════════════
#                              REDEEM
# ═══════════════════════════════════════════════════════════════════════

async def cb_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data["state"] = "redeem_code"
    await send_or_edit(
        update,
        "🎁 <b>Redeem Code</b>\n\nEnter your redeem code:",
        kb([[("❌ Cancel", "home:main")]]),
    )


async def handle_redeem_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    import json as _j
    code = update.message.text.strip()
    r = db.get_redeem_code(code)
    if not r:
        await update.message.reply_text("❌ Invalid code.")
        return
    if r["expiry"]:
        try:
            if datetime.fromisoformat(r["expiry"]) < datetime.now(timezone.utc):
                await update.message.reply_text("❌ Code expired.")
                return
        except Exception:
            pass
    if r["used_count"] >= r["usage_limit"]:
        await update.message.reply_text("❌ Code usage limit reached.")
        return
    used = _j.loads(r["used_by"])
    uid = update.effective_user.id
    if uid in used:
        await update.message.reply_text("❌ You already used this code.")
        return
    db.use_redeem(code, uid)
    db.add_balance(uid, r["amount"], f"Redeem {code}")
    context.user_data.pop("state", None)
    await update.message.reply_html(
        f"🎉 <b>Redeem Successful!</b>\n\n"
        f"💰 {money(r['amount'])} added to your wallet.\n"
        f"👛 New balance: <b>{money(db.get_balance(uid))}</b>",
        reply_markup=home_kb(uid),
    )
    await log_channel(
        context, f"🎁 Redeem <code>{code}</code> used by <code>{uid}</code>"
    )


# ═══════════════════════════════════════════════════════════════════════
#                              SUPPORT / UPDATES
# ═══════════════════════════════════════════════════════════════════════

async def cb_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    text = (
        f"❓ <b>Support</b>\n\n"
        f"Need help? Reach us here:\n"
        f"👉 @{esc(SUPPORT_USERNAME)}"
    )
    await send_or_edit(update, text, kb([
        [("💬 Message Support", f"https://t.me/{SUPPORT_USERNAME}")],
        [("🔙 Back", "home:main")],
    ]) if False else kb([[("🔙 Back", "home:main")]]))


async def cb_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    text = (
        f"📢 <b>Updates Channel</b>\n\n"
        f"Stay updated → @{esc(STORE_USERNAME)}"
    )
    await send_or_edit(update, text, kb([[("🔙 Back", "home:main")]]))


# ═══════════════════════════════════════════════════════════════════════
#                              OWNER PANEL
# ═══════════════════════════════════════════════════════════════════════

async def cmd_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_html(
        "⚙️ <b>Owner Panel</b>", reply_markup=owner_kb()
    )

def owner_kb() -> InlineKeyboardMarkup:
    return kb([
        [("📦 Products",     "own:products"),
         ("📋 Orders",       "own:orders")],
        [("👥 Users",        "own:users"),
         ("💰 Wallet",       "own:wallet")],
        [("📊 Statistics",   "own:stats"),
         ("📢 Broadcast",    "own:bcast")],
        [("🎁 Redeem Codes", "own:redeem"),
         ("⚙ Settings",     "own:settings")],
        [("🔙 Home",         "home:main")],
    ])

async def cb_owner_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Not authorized", show_alert=True); return
    await q.answer()
    await send_or_edit(update, "⚙️ <b>Owner Panel</b>", owner_kb())


# ─── Owner: Products ────────────────────────────────────────────────
async def cb_own_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    products = db.all_products()
    rows = [[("➕ Add Product", "prod:add")]]
    for p in products[:30]:
        rows.append([(f"{p['name']} — {money(p['price'])}", f"prodm:{p['id']}")])
    rows.append([("🔙 Back", "owner:home")])
    await send_or_edit(
        update, f"📦 <b>Products</b> ({len(products)} total)", kb(rows)
    )


async def cb_prod_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    p = db.get_product(pid)
    if not p:
        return
    import json as _j
    files = _j.loads(p["files_json"] or "[]")
    text = (
        f"💎 <b>{esc(p['name'])}</b>\n\n"
        f"📂 Category: {esc(p['category'])}\n"
        f"💰 Price: {money(p['price'])}\n"
        f"🗂 Files: {len(files)}\n\n"
        f"📝 {esc(p['description'] or '—')}"
    )
    await send_or_edit(update, text, kb([
        [("✏️ Name",       f"pedit:name:{pid}"),
         ("📝 Description", f"pedit:desc:{pid}")],
        [("📂 Category",   f"pedit:cat:{pid}"),
         ("💰 Price",       f"pedit:price:{pid}")],
        [("🖼 Preview",    f"pedit:preview:{pid}"),
         ("🗂 Files",      f"pedit:files:{pid}")],
        [("🗑 Delete",     f"pdel:{pid}"),
         ("🔙 Back",       "own:products")],
    ]))


async def cb_prod_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    context.user_data["state"] = "add_prod_name"
    context.user_data["new_prod"] = {}
    await send_or_edit(
        update, "➕ <b>Add Product</b>\n\nStep 1/6: Send product <b>name</b>:",
        kb([[("❌ Cancel", "owner:home")]]),
    )


async def cb_prod_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    pid = int(q.data.split(":", 1)[1])
    await q.answer()
    await send_or_edit(
        update, "⚠️ Delete this product? This cannot be undone.",
        kb([
            [("✅ Confirm Delete", f"pdelc:{pid}")],
            [("🔙 Cancel", f"prodm:{pid}")],
        ]),
    )


async def cb_prod_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    pid = int(q.data.split(":", 1)[1])
    db.delete_product(pid)
    await q.answer("Deleted")
    await send_or_edit(
        update, "🗑 Product deleted.",
        kb([[("🔙 Back", "own:products")]]),
    )


async def cb_prod_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    _, field, pid = q.data.split(":")
    context.user_data["edit_prod"] = {"pid": int(pid), "field": field}
    await q.answer()
    prompts = {
        "name":    "Send new <b>name</b>:",
        "desc":    "Send new <b>description</b>:",
        "price":   "Send new <b>price</b>:",
        "cat":     "Choose new <b>category</b>:",
        "preview": "Send new <b>preview</b> (photo or video):",
        "files":   "Send <b>new files</b> one by one. Send /done when finished.",
    }
    if field == "cat":
        rows = [[(c, f"pcat:{pid}:{c}")] for c in CATEGORIES]
        rows.append([("🔙 Back", f"prodm:{pid}")])
        await send_or_edit(update, prompts[field], kb(rows))
        return
    context.user_data["state"] = f"edit_prod_{field}"
    if field == "files":
        context.user_data["edit_prod_files"] = []
    await send_or_edit(
        update, f"✏️ {prompts[field]}",
        kb([[("❌ Cancel", f"prodm:{pid}")]]),
    )


async def cb_prod_setcat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    _, pid, cat = q.data.split(":", 2)
    db.update_product(int(pid), "category", cat)
    await q.answer("Category updated")
    await send_or_edit(
        update, f"✅ Category set to <b>{esc(cat)}</b>.",
        kb([[("🔙 Back", f"prodm:{pid}")]]),
    )


# ─── Owner: Orders ──────────────────────────────────────────────────
async def cb_own_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    orders = db.all_orders(30)
    if not orders:
        await send_or_edit(update, "📋 No orders yet.",
                           kb([[("🔙 Back", "owner:home")]])); return
    lines = ["📋 <b>Recent Orders</b>", ""]
    for o in orders:
        p = db.get_product(o["product_id"])
        pname = p["name"] if p else "—"
        lines.append(
            f"• <code>{o['order_id']}</code> • {esc(pname)} • "
            f"{money(o['price'])} • {o['status']}"
        )
    await send_or_edit(update, "\n".join(lines),
                       kb([[("🔙 Back", "owner:home")]]))


# ─── Owner: Users ───────────────────────────────────────────────────
async def cb_own_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    users = db.all_users()
    lines = [f"👥 <b>Users:</b> {len(users)}", ""]
    for u in users[:30]:
        lines.append(
            f"• <code>{u['user_id']}</code> "
            f"@{esc(u['username'] or '—')} • "
            f"{money(u['balance'])}"
        )
    if len(users) > 30:
        lines.append(f"…and {len(users) - 30} more")
    await send_or_edit(update, "\n".join(lines),
                       kb([[("🔙 Back", "owner:home")]]))


# ─── Owner: Wallet Mgmt ─────────────────────────────────────────────
async def cb_own_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    await send_or_edit(update, "💰 <b>Wallet Management</b>", kb([
        [("💳 Add Balance",   "ow:add")],
        [("➖ Remove Balance", "ow:rem")],
        [("✏ Set Balance",   "ow:set")],
        [("🔍 Check Balance", "ow:check")],
        [("📜 History",       "ow:hist")],
        [("🔙 Back",          "owner:home")],
    ]))


async def cb_own_wallet_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    action = q.data.split(":", 1)[1]
    await q.answer()
    context.user_data["ow_action"] = action
    context.user_data["state"] = "ow_search_user"
    await send_or_edit(
        update,
        "🔍 Send <b>User ID</b> or <b>@username</b>:",
        kb([[("❌ Cancel", "own:wallet")]]),
    )


# ─── Owner: Stats ───────────────────────────────────────────────────
async def cb_own_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    s = db.stats()
    text = (
        "📊 <b>Statistics</b>\n\n"
        f"👥 Total Users: <b>{s['users']}</b>\n"
        f"🆕 Today's Users: <b>{s['today']}</b>\n"
        f"📦 Products Sold: <b>{s['sold']}</b>\n"
        f"💰 Revenue: <b>{money(s['revenue'])}</b>\n"
        f"⏳ Pending Payments: <b>{s['pending']}</b>\n"
        f"✅ Completed Payments: <b>{s['completed']}</b>\n"
        f"💸 Wallet Requests (pending): <b>{s['wreqs']}</b>\n"
        f"🛒 Total Orders: <b>{s['orders']}</b>"
    )
    await send_or_edit(update, text,
                       kb([[("🔙 Back", "owner:home")]]))


# ─── Owner: Broadcast ───────────────────────────────────────────────
async def cb_own_bcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    context.user_data["state"] = "broadcast_content"
    await send_or_edit(
        update,
        "📢 <b>Broadcast</b>\n\n"
        "Send the message (text, photo, video, animation or document) you "
        "want to broadcast to all users. Any inline buttons on the message "
        "will be preserved.",
        kb([[("❌ Cancel", "owner:home")]]),
    )


async def do_broadcast(
    context: ContextTypes.DEFAULT_TYPE, source_msg, owner_chat_id: int
) -> None:
    users = db.all_users()
    ok = fail = 0
    for u in users:
        try:
            await source_msg.copy(chat_id=u["user_id"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await context.bot.send_message(
        owner_chat_id,
        f"✅ Broadcast finished.\nDelivered: {ok}\nFailed: {fail}",
    )
    await log_channel(context, f"📢 Broadcast sent — ok:{ok} fail:{fail}")


# ─── Owner: Redeem ──────────────────────────────────────────────────
async def cb_own_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    codes = db.all_redeem_codes()
    rows = [[("➕ New Code", "rd:new")]]
    for c in codes[:20]:
        rows.append([(
            f"{c['code']} • {money(c['amount'])} • "
            f"{c['used_count']}/{c['usage_limit']}",
            f"rd:view:{c['code']}",
        )])
    rows.append([("🔙 Back", "owner:home")])
    await send_or_edit(update,
                       f"🎁 <b>Redeem Codes</b> ({len(codes)})", kb(rows))


async def cb_redeem_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    context.user_data["state"] = "rd_new_code"
    context.user_data["new_rd"] = {}
    await send_or_edit(
        update,
        "🎁 Step 1/4: Send the <b>code text</b> (e.g. <code>WELCOME50</code>):",
        kb([[("❌ Cancel", "own:redeem")]]),
    )


async def cb_redeem_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    code = q.data.split(":", 2)[2]
    r = db.get_redeem_code(code)
    if not r:
        await q.answer("Not found", show_alert=True); return
    await q.answer()
    text = (
        f"🎁 <b>{esc(code)}</b>\n\n"
        f"💰 Amount: {money(r['amount'])}\n"
        f"🔁 Used: {r['used_count']}/{r['usage_limit']}\n"
        f"📅 Expiry: {r['expiry'] or 'Never'}"
    )
    await send_or_edit(update, text, kb([
        [("🗑 Delete", f"rd:del:{code}")],
        [("🔙 Back",   "own:redeem")],
    ]))


async def cb_redeem_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    code = q.data.split(":", 2)[2]
    db.delete_redeem_code(code)
    await q.answer("Deleted")
    await send_or_edit(update, "🗑 Code deleted.",
                       kb([[("🔙 Back", "own:redeem")]]))


# ─── Owner: Settings ────────────────────────────────────────────────
async def cb_own_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer(); return
    await q.answer()
    text = (
        "⚙ <b>Settings</b>\n\n"
        f"🏪 Store: {esc(STORE_NAME)}\n"
        f"👤 Owner ID: <code>{OWNER_ID}</code>\n"
        f"🏦 UPI ID: <code>{esc(UPI_ID)}</code>\n"
        f"📢 Log Channel: <code>{LOG_CHANNEL_ID or 'disabled'}</code>\n"
        f"📚 Support: @{esc(SUPPORT_USERNAME)}\n"
        f"📣 Updates: @{esc(STORE_USERNAME)}\n\n"
        "Edit these values at the top of <code>bot.py</code> and restart."
    )
    await send_or_edit(update, text,
                       kb([[("🔙 Back", "owner:home")]]))


# ═══════════════════════════════════════════════════════════════════════
#                          TEXT / MEDIA ROUTER
# ═══════════════════════════════════════════════════════════════════════

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("state")
    uid = update.effective_user.id
    text = update.message.text.strip()

    # ── user flows ─────────────────────────────────────────────────
    if state == "add_money_amount":
        await handle_add_money_amount(update, context); return
    if state == "redeem_code":
        await handle_redeem_code(update, context); return

    # ── owner: reject reasons ──────────────────────────────────────
    if state == "reject_order_reason" and is_owner(uid):
        oid = context.user_data.pop("reject_order_id", None)
        context.user_data.pop("state", None)
        if oid:
            o = db.get_order(oid)
            db.update_order(oid, status="rejected")
            if o:
                try:
                    await context.bot.send_message(
                        o["user_id"],
                        f"❌ Your order <code>{oid}</code> was rejected.\n\n"
                        f"Reason: {esc(text)}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            await update.message.reply_text("Rejection sent.")
        return

    if state == "reject_wallet_reason" and is_owner(uid):
        req_id = context.user_data.pop("reject_wallet_id", None)
        context.user_data.pop("state", None)
        if req_id:
            r = db.get_wallet_request(req_id)
            db.update_wallet_request(req_id, "rejected")
            if r:
                try:
                    await context.bot.send_message(
                        r["user_id"],
                        f"❌ Your wallet top-up request was rejected.\n\n"
                        f"Reason: {esc(text)}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            await update.message.reply_text("Rejection sent.")
        return

    # ── owner: add product flow ────────────────────────────────────
    if state == "add_prod_name" and is_owner(uid):
        context.user_data["new_prod"]["name"] = text
        context.user_data["state"] = "add_prod_desc"
        await update.message.reply_html(
            "Step 2/6: Send product <b>description</b>:"
        ); return

    if state == "add_prod_desc" and is_owner(uid):
        context.user_data["new_prod"]["description"] = text
        context.user_data["state"] = "add_prod_cat"
        rows = [[(c, f"npcat:{c}")] for c in CATEGORIES]
        await update.message.reply_html(
            "Step 3/6: Choose <b>category</b>:", reply_markup=kb(rows),
        ); return

    if state == "add_prod_price" and is_owner(uid):
        try:
            price = float(text)
            if price < 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("Invalid price."); return
        context.user_data["new_prod"]["price"] = price
        context.user_data["state"] = "add_prod_preview"
        await update.message.reply_html(
            "Step 5/6: Send <b>preview</b> (photo or video). "
            "Send /skip to skip:",
        ); return

    # ── owner: edit product ────────────────────────────────────────
    if state and state.startswith("edit_prod_") and is_owner(uid):
        field = state.replace("edit_prod_", "")
        ep = context.user_data.get("edit_prod", {})
        pid = ep.get("pid")
        if not pid:
            return
        if field == "name":
            db.update_product(pid, "name", text)
        elif field == "desc":
            db.update_product(pid, "description", text)
        elif field == "price":
            try:
                db.update_product(pid, "price", float(text))
            except Exception:
                await update.message.reply_text("Invalid price."); return
        else:
            return
        context.user_data.pop("state", None)
        await update.message.reply_html(
            "✅ Updated.", reply_markup=kb([[("🔙 Back", f"prodm:{pid}")]]),
        ); return

    # ── owner: wallet mgmt ────────────────────────────────────────
    if state == "ow_search_user" and is_owner(uid):
        u = db.find_user(text)
        if not u:
            await update.message.reply_text("User not found."); return
        context.user_data["ow_target"] = u["user_id"]
        action = context.user_data.get("ow_action")
        if action == "check":
            await update.message.reply_html(
                f"👤 <b>{esc(u['first_name'] or '')}</b>\n"
                f"🆔 <code>{u['user_id']}</code>\n"
                f"💰 Balance: <b>{money(u['balance'])}</b>",
                reply_markup=kb([[("🔙 Back", "own:wallet")]]),
            )
            context.user_data.pop("state", None); return
        if action == "hist":
            rows = db.wallet_history(u["user_id"], 30)
            if not rows:
                await update.message.reply_text("No history."); return
            lines = [f"📜 History for <code>{u['user_id']}</code>", ""]
            for r in rows:
                sign = "➕" if r["type"] == "credit" else "➖"
                lines.append(
                    f"{sign} {money(abs(r['amount']))} — {esc(r['note'])} "
                    f"<i>({fmt_dt(r['created_at'])})</i>"
                )
            await update.message.reply_html(
                "\n".join(lines),
                reply_markup=kb([[("🔙 Back", "own:wallet")]]),
            )
            context.user_data.pop("state", None); return
        context.user_data["state"] = "ow_amount"
        await update.message.reply_html(
            f"👤 Target: <code>{u['user_id']}</code>\n\n"
            f"Send amount:"
        ); return

    if state == "ow_amount" and is_owner(uid):
        try:
            amount = float(text)
        except Exception:
            await update.message.reply_text("Invalid amount."); return
        target = context.user_data.get("ow_target")
        action = context.user_data.get("ow_action")
        if action == "add":
            db.add_balance(target, amount, "Owner credit")
            msg = f"➕ Added {money(amount)}"
        elif action == "rem":
            db.add_balance(target, -amount, "Owner debit")
            msg = f"➖ Removed {money(amount)}"
        elif action == "set":
            db.set_balance(target, amount, "Owner set balance")
            msg = f"✏ Balance set to {money(amount)}"
        else:
            return
        context.user_data.pop("state", None)
        await update.message.reply_html(
            f"✅ {msg} for user <code>{target}</code>.\n"
            f"New balance: <b>{money(db.get_balance(target))}</b>",
            reply_markup=kb([[("🔙 Back", "own:wallet")]]),
        )
        try:
            await context.bot.send_message(
                target,
                f"💰 Wallet updated by admin.\n"
                f"New balance: <b>{money(db.get_balance(target))}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await log_channel(context, f"{msg} → <code>{target}</code>")
        return

    # ── owner: redeem creation ────────────────────────────────────
    if state == "rd_new_code" and is_owner(uid):
        context.user_data["new_rd"]["code"] = text
        context.user_data["state"] = "rd_new_amount"
        await update.message.reply_html("Step 2/4: Send <b>amount</b>:"); return
    if state == "rd_new_amount" and is_owner(uid):
        try:
            context.user_data["new_rd"]["amount"] = float(text)
        except Exception:
            await update.message.reply_text("Invalid amount."); return
        context.user_data["state"] = "rd_new_limit"
        await update.message.reply_html("Step 3/4: Send <b>usage limit</b>:"); return
    if state == "rd_new_limit" and is_owner(uid):
        try:
            context.user_data["new_rd"]["limit"] = int(text)
        except Exception:
            await update.message.reply_text("Invalid number."); return
        context.user_data["state"] = "rd_new_expiry"
        await update.message.reply_html(
            "Step 4/4: Send <b>expiry</b> as <code>YYYY-MM-DD</code> "
            "or send /skip for no expiry."
        ); return
    if state == "rd_new_expiry" and is_owner(uid):
        expiry = None
        if text.lower() not in ("/skip", "skip", "none"):
            try:
                expiry = datetime.strptime(text, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc,
                ).isoformat()
            except Exception:
                await update.message.reply_text("Invalid date."); return
        d = context.user_data["new_rd"]
        try:
            db.add_redeem_code(d["code"], d["amount"], expiry, d["limit"])
        except sqlite3.IntegrityError:
            await update.message.reply_text("Code already exists."); return
        context.user_data.pop("state", None)
        context.user_data.pop("new_rd", None)
        await update.message.reply_html(
            f"✅ Code <code>{esc(d['code'])}</code> created.",
            reply_markup=kb([[("🔙 Back", "own:redeem")]]),
        ); return

    # ── owner: broadcast text ────────────────────────────────────
    if state == "broadcast_content" and is_owner(uid):
        context.user_data.pop("state", None)
        await update.message.reply_text("📢 Broadcasting…")
        await do_broadcast(context, update.message, uid)
        return


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles photos, videos, animations, documents based on state."""
    state = context.user_data.get("state")
    uid = update.effective_user.id
    msg = update.message

    if state == "purchase_screenshot" and msg.photo:
        await handle_purchase_screenshot(update, context); return
    if state == "add_money_screenshot" and msg.photo:
        await handle_topup_screenshot(update, context); return

    # ── owner: broadcast media ───────────────────────────────────
    if state == "broadcast_content" and is_owner(uid):
        context.user_data.pop("state", None)
        await msg.reply_text("📢 Broadcasting…")
        await do_broadcast(context, msg, uid)
        return

    # ── owner: add-product preview / files ───────────────────────
    if state == "add_prod_preview" and is_owner(uid):
        if msg.photo:
            context.user_data["new_prod"]["preview_type"] = "photo"
            context.user_data["new_prod"]["preview_id"] = msg.photo[-1].file_id
        elif msg.video:
            context.user_data["new_prod"]["preview_type"] = "video"
            context.user_data["new_prod"]["preview_id"] = msg.video.file_id
        else:
            await msg.reply_text("Send a photo or video, or /skip.")
            return
        context.user_data["state"] = "add_prod_files"
        context.user_data["new_prod"]["files"] = []
        await msg.reply_html(
            "Step 6/6: Send <b>files</b> one by one (photo/video/animation/"
            "document). Send /done when finished."
        ); return

    if state == "add_prod_files" and is_owner(uid):
        f = _extract_file(msg)
        if not f:
            await msg.reply_text("Unsupported. Send a photo/video/animation/document.")
            return
        context.user_data["new_prod"]["files"].append(f)
        await msg.reply_text(
            f"✅ Added ({len(context.user_data['new_prod']['files'])}). "
            f"Send more or /done."
        ); return

    # ── owner: edit product preview / files ──────────────────────
    if state == "edit_prod_preview" and is_owner(uid):
        ep = context.user_data.get("edit_prod", {})
        pid = ep.get("pid")
        if not pid:
            return
        if msg.photo:
            db.update_product(pid, "preview_type", "photo")
            db.update_product(pid, "preview_id", msg.photo[-1].file_id)
        elif msg.video:
            db.update_product(pid, "preview_type", "video")
            db.update_product(pid, "preview_id", msg.video.file_id)
        else:
            await msg.reply_text("Send photo or video."); return
        context.user_data.pop("state", None)
        await msg.reply_html(
            "✅ Preview updated.",
            reply_markup=kb([[("🔙 Back", f"prodm:{pid}")]]),
        ); return

    if state == "edit_prod_files" and is_owner(uid):
        f = _extract_file(msg)
        if not f:
            await msg.reply_text("Unsupported file type."); return
        context.user_data.setdefault("edit_prod_files", []).append(f)
        await msg.reply_text(
            f"✅ Added ({len(context.user_data['edit_prod_files'])}). "
            f"Send more or /done."
        ); return


def _extract_file(msg) -> Optional[dict]:
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id, "name": ""}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id,
                "name": msg.video.file_name or ""}
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id,
                "name": msg.animation.file_name or ""}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id,
                "name": msg.document.file_name or ""}
    return None


# ── /skip and /done inside flows ─────────────────────────────────
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("state")
    uid = update.effective_user.id
    if state == "add_prod_preview" and is_owner(uid):
        context.user_data["new_prod"]["preview_type"] = None
        context.user_data["new_prod"]["preview_id"] = None
        context.user_data["state"] = "add_prod_files"
        context.user_data["new_prod"]["files"] = []
        await update.message.reply_html(
            "Step 6/6: Send files one by one. Send /done when finished."
        ); return
    if state == "rd_new_expiry" and is_owner(uid):
        # simulate skip
        update.message.text = "/skip"
        await on_text(update, context); return


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import json as _j
    state = context.user_data.get("state")
    uid = update.effective_user.id

    if state == "add_prod_files" and is_owner(uid):
        np = context.user_data["new_prod"]
        pid = db.add_product(
            name=np["name"], description=np["description"],
            category=np["category"], price=np["price"],
            preview_type=np.get("preview_type"),
            preview_id=np.get("preview_id"),
            files_json=_j.dumps(np.get("files", [])),
        )
        context.user_data.pop("state", None)
        context.user_data.pop("new_prod", None)
        await update.message.reply_html(
            f"✅ Product added! ID: <code>{pid}</code>",
            reply_markup=kb([[("🔙 Back", "own:products")]]),
        ); return

    if state == "edit_prod_files" and is_owner(uid):
        ep = context.user_data.get("edit_prod", {})
        pid = ep.get("pid")
        files = context.user_data.get("edit_prod_files", [])
        if pid:
            existing = _j.loads(db.get_product(pid)["files_json"] or "[]")
            existing.extend(files)
            db.update_product(pid, "files_json", _j.dumps(existing))
        context.user_data.pop("state", None)
        context.user_data.pop("edit_prod_files", None)
        await update.message.reply_html(
            "✅ Files added.",
            reply_markup=kb([[("🔙 Back", f"prodm:{pid}")]]),
        ); return


# ═══════════════════════════════════════════════════════════════════════
#                          MASTER CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════════════════

async def callback_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    data = q.data or ""
    try:
        # HOME
        if data == "home:main":
            await cb_home(update, context); return
        if data == "home:shop":
            await cb_shop(update, context); return
        if data == "home:wallet":
            await cb_wallet(update, context); return
        if data == "home:purchased":
            await cb_purchased(update, context); return
        if data == "home:orders":
            await cb_orders(update, context); return
        if data == "home:redeem":
            await cb_redeem(update, context); return
        if data == "home:support":
            await cb_support(update, context); return
        if data == "home:updates":
            await cb_updates(update, context); return

        # SHOP
        if data.startswith("cat:"):
            await cb_category(update, context); return
        if data.startswith("prod:") and data != "prod:add":
            await cb_product(update, context); return
        if data.startswith("buy:"):
            await cb_buy(update, context); return
        if data.startswith("paywallet:"):
            await cb_pay_wallet(update, context); return
        if data.startswith("payupi:"):
            await cb_pay_upi(update, context); return
        if data.startswith("pdone:"):
            await cb_pdone(update, context); return
        if data.startswith("pcancel:"):
            await cb_pcancel(update, context); return

        # OWNER ORDER APPROVALS
        if data.startswith("oapprove:"):
            await cb_owner_approve(update, context); return
        if data.startswith("oreject:"):
            await cb_owner_reject(update, context); return

        # WALLET
        if data == "wallet:add":
            await cb_wallet_add(update, context); return
        if data == "wallet:history":
            await cb_wallet_history(update, context); return
        if data == "topup:done":
            await cb_topup_done(update, context); return
        if data.startswith("wapprove:"):
            await cb_wallet_approve(update, context); return
        if data.startswith("wreject:"):
            await cb_wallet_reject(update, context); return

        # PURCHASED
        if data.startswith("redl:"):
            await cb_redownload(update, context); return

        # OWNER PANEL
        if data == "owner:home":
            await cb_owner_home(update, context); return
        if data == "own:products":
            await cb_own_products(update, context); return
        if data == "prod:add":
            await cb_prod_add(update, context); return
        if data.startswith("prodm:"):
            await cb_prod_manage(update, context); return
        if data.startswith("pdel:"):
            await cb_prod_delete(update, context); return
        if data.startswith("pdelc:"):
            await cb_prod_delete_confirm(update, context); return
        if data.startswith("pedit:"):
            await cb_prod_edit(update, context); return
        if data.startswith("pcat:"):
            await cb_prod_setcat(update, context); return
        if data.startswith("npcat:"):
            # new-product category chosen
            if not is_owner(q.from_user.id):
                await q.answer(); return
            cat = data.split(":", 1)[1]
            context.user_data["new_prod"]["category"] = cat
            context.user_data["state"] = "add_prod_price"
            await q.answer()
            await q.message.reply_html("Step 4/6: Send <b>price</b>:"); return

        if data == "own:orders":
            await cb_own_orders(update, context); return
        if data == "own:users":
            await cb_own_users(update, context); return
        if data == "own:wallet":
            await cb_own_wallet(update, context); return
        if data.startswith("ow:"):
            await cb_own_wallet_action(update, context); return
        if data == "own:stats":
            await cb_own_stats(update, context); return
        if data == "own:bcast":
            await cb_own_bcast(update, context); return
        if data == "own:redeem":
            await cb_own_redeem(update, context); return
        if data == "rd:new":
            await cb_redeem_new(update, context); return
        if data.startswith("rd:view:"):
            await cb_redeem_view(update, context); return
        if data.startswith("rd:del:"):
            await cb_redeem_del(update, context); return
        if data == "own:settings":
            await cb_own_settings(update, context); return

        await q.answer("Unknown action", show_alert=False)
    except Exception as e:
        logger.exception("callback error: %s", e)
        try:
            await q.answer("An error occurred.", show_alert=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#                              MAIN
# ═══════════════════════════════════════════════════════════════════════

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled: %s", context.error)


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("owner", cmd_owner))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("done", cmd_done))

    app.add_handler(CallbackQueryHandler(callback_router))

    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
        on_media,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ BOT_TOKEN is empty. Edit the CONFIG section at the top "
            "of bot.py before running."
        )
    logger.info("Starting %s…", STORE_NAME)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
