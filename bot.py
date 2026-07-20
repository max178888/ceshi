import os
import random
import sqlite3
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler

# ========== 配置 ==========
TOKEN = "8179579064:AAHPZAhthw4i_YFFe3UerGrNmoJc2wWKd5g"  # 请替换为实际Token

ALLOWED_GROUPS_STR = "-1003720878201,-1003002241602,-1003745425265,-1003779300640"
ALLOWED_GROUPS = [int(g.strip()) for g in ALLOWED_GROUPS_STR.split(",")]

ADMIN_IDS = [8354445328, 877039616]   # 管理员ID

BASE_DROP_PROB = 0.14

def get_dynamic_drop_prob(today_gain):
    if today_gain >= 20:
        return 0.02
    elif today_gain >= 17:
        return 0.05
    elif today_gain >= 15:
        return 0.06
    elif today_gain >= 10:
        return 0.1
    else:
        return BASE_DROP_PROB

TRIPLE_MULTIPLIER = 3
DB_PATH = "/data/credits.db"

# 商品列表：id, 名称, 价格
SHOP = [
    (1, "100元福利券", 500),
    (2, "3个月TG会员", 1000),
    (3, "琪琪半价券", 300),   # 全局限量1张，价格300
]

INTERVALS = [
    (0.50, 0.0, 0.3),
    (0.20, 0.3, 0.6),
    (0.10, 0.6, 0.8),
    (0.10, 0.8, 1.0),
    (0.10, 1.0, 2.0),
    (0.085, 2.0, 4.0),
    (0.01, 4.0, 6.0),
    (0.005, 6.0, 8.0),
]

def rand_coin():
    r = random.random()
    cum = 0.0
    for prob, low, high in INTERVALS:
        cum += prob
        if r <= cum:
            return round(random.uniform(low, high), 2)
    return round(random.uniform(0, 8), 2)

# ========== 数据库 ==========
def db_connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INT PRIMARY KEY, nickname TEXT, coins REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS daily (user_id INT, date TEXT, gain REAL, PRIMARY KEY(user_id, date))")
        c.execute("CREATE TABLE IF NOT EXISTS tx (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, type TEXT, amount REAL, desc TEXT, ts TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS daily_first_bonus (user_id INT, date TEXT, used INT DEFAULT 0, PRIMARY KEY(user_id, date))")
        c.execute("CREATE TABLE IF NOT EXISTS limited_purchases (user_id INT, item_id INT, PRIMARY KEY(user_id, item_id))")
        # 全局限量表：item_id 是否已售出
        c.execute("CREATE TABLE IF NOT EXISTS global_limits (item_id INT PRIMARY KEY, sold INT DEFAULT 0)")
        # 初始化琪琪半价券 (item_id=3) 未售出
        c.execute("INSERT OR IGNORE INTO global_limits (item_id, sold) VALUES (3, 0)")
        conn.commit()

def get_user(uid, name):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,))
        if not c.fetchone():
            c.execute("INSERT INTO users (user_id, nickname, coins) VALUES (?,?,0)", (uid, name))
        else:
            c.execute("UPDATE users SET nickname=? WHERE user_id=?", (name, uid))
        conn.commit()

def get_today_gain(uid):
    today = datetime.now().strftime('%Y-%m-%d')
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT gain FROM daily WHERE user_id=? AND date=?", (uid, today))
        row = c.fetchone()
        return row[0] if row else 0.0

def add_today_gain(uid, amt):
    today = datetime.now().strftime('%Y-%m-%d')
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO daily (user_id, date, gain) VALUES (?,?,?) ON CONFLICT(user_id,date) DO UPDATE SET gain = gain + ?",
                  (uid, today, amt, amt))
        conn.commit()

def add_coins(uid, amt, reason):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (amt, uid))
        if c.rowcount == 0:
            c.execute("INSERT INTO users (user_id, nickname, coins) VALUES (?,?,?)", (uid, "未知", amt))
        c.execute("INSERT INTO tx (user_id, type, amount, desc, ts) VALUES (?,?,?,?,?)",
                  (uid, "收入" if amt > 0 else "支出", abs(amt), reason, datetime.now()))
        conn.commit()

def sub_coins(uid, amt, reason):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT coins FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        if not row or row[0] < amt:
            return False
        c.execute("UPDATE users SET coins = coins - ? WHERE user_id=?", (amt, uid))
        c.execute("INSERT INTO tx (user_id, type, amount, desc, ts) VALUES (?,?,?,?,?)",
                  (uid, "支出", amt, reason, datetime.now()))
        conn.commit()
        return True

def get_coins(uid):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT coins FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        return row[0] if row else 0.0

def history(uid, limit=10):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT type, amount, desc, ts FROM tx WHERE user_id=? ORDER BY ts DESC LIMIT ?", (uid, limit))
        return c.fetchall() or []

def check_and_use_first_bonus(uid):
    today = datetime.now().strftime('%Y-%m-%d')
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT used FROM daily_first_bonus WHERE user_id=? AND date=?", (uid, today))
    row = c.fetchone()
    if row and row[0] == 1:
        conn.close()
        return False
    c.execute("INSERT OR REPLACE INTO daily_first_bonus (user_id, date, used) VALUES (?,?,1)", (uid, today))
    conn.commit()
    conn.close()
    return True

# ---------- 全局限量购买 ----------
def is_global_sold(item_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT sold FROM global_limits WHERE item_id=?", (item_id,))
    row = c.fetchone()
    conn.close()
    return row and row[0] == 1

def mark_global_sold(item_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE global_limits SET sold=1 WHERE item_id=?", (item_id,))
    conn.commit()
    conn.close()

# ========== 键盘 ==========
def wallet_kb():
    return Markup([
        [Btn("🛒 兑换商品", callback_data="shop"),
         Btn("📚 学分记录", callback_data="history")]
    ])

def shop_kb():
    return Markup([
        [Btn(f"{n} - {p}💎", callback_data=f"buy_{i}")] for i, n, p in SHOP
    ])

# ========== 群聊消息处理 ==========
async def on_msg(update, ctx):
    if update.message.from_user.is_bot:
        return
    if update.effective_chat.type not in ('group', 'supergroup'):
        return
    if update.effective_chat.id not in ALLOWED_GROUPS:
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if text == "商城":
        uid = update.message.from_user.id
        name = update.message.from_user.first_name
        get_user(uid, name)
        bal = get_coins(uid)
        await update.message.reply_text(
            f"🛒 学分商城\n💎 当前余额：{bal:.2f} 学分\n点击下方按钮兑换商品：",
            reply_markup=shop_kb()
        )
        return
    if text.startswith('/'):
        return
    if len(text) < 4:
        return

    uid = update.message.from_user.id
    name = update.message.from_user.first_name
    get_user(uid, name)

    today_gain = get_today_gain(uid)
    base_prob = get_dynamic_drop_prob(today_gain)
    use_bonus = check_and_use_first_bonus(uid)
    current_prob = base_prob * TRIPLE_MULTIPLIER if use_bonus else base_prob
    current_prob = min(current_prob, 1.0)

    if random.random() < current_prob:
        coin = rand_coin()
        add_coins(uid, coin, "发言掉落")
        add_today_gain(uid, coin)
        bal = get_coins(uid)
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        await update.message.reply_text(
            f"🧧恭喜 {link} 中奖！\n💰获得：{coin:.2f} 学分\n📚余额：{bal:.2f} 学分\n💡发送「商城」可兑换商品",
            parse_mode=ParseMode.HTML
        )

# ========== 回调处理器 ==========
async def cb(update, ctx):
    if update.callback_query.from_user.is_bot:
        return
    if update.effective_chat.type not in ('group', 'supergroup') or update.effective_chat.id not in ALLOWED_GROUPS:
        return
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.from_user.first_name
    data = query.data

    if data == "shop":
        bal = get_coins(uid)
        await query.edit_message_text(
            f"🛒 学分商城\n💎 当前余额：{bal:.2f} 学分\n点击下方按钮兑换商品：",
            reply_markup=shop_kb()
        )
    elif data == "history":
        rows = history(uid)
        if not rows:
            txt = "📭 暂无学分记录"
        else:
            lines = []
            for typ, amt, desc, ts in rows:
                sign = "✅ +" if typ == "收入" else "❌ -"
                lines.append(f"{sign}{amt:.2f}  {desc}  {ts[:16]}")
            txt = "📋 最近学分记录：\n\n" + "\n".join(lines)
        await query.edit_message_text(txt, reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]]))
    elif data == "back":
        bal = get_coins(uid)
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        await query.edit_message_text(
            f"👛 我的钱包\n\n用户：{link}\n余额：{bal:.2f} 学分",
            reply_markup=wallet_kb(),
            parse_mode=ParseMode.HTML
        )
    elif data.startswith("buy_"):
        iid = int(data.split("_")[1])
        item = next((i for i in SHOP if i[0] == iid), None)
        if not item:
            await query.edit_message_text("❌ 商品不存在")
            return
        _, n, p = item
        current = get_coins(uid)

        # 全局限量商品检查（id=3 琪琪半价券）
        if iid == 3:
            if is_global_sold(3):
                await query.edit_message_text(
                    "❌ 琪琪半价券已售罄！只有一张，已经被其他人买走了。",
                    reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]])
                )
                return

        if current < p:
            await query.edit_message_text(
                f"❌ 学分不足！需要 {p} 学分，你只有 {current:.2f} 学分",
                reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]])
            )
            return

        if sub_coins(uid, p, f"购买 {n}"):
            if iid == 3:
                mark_global_sold(3)   # 标记全局售出
            new_balance = get_coins(uid)
            await query.edit_message_text(
                f"✅ {n} 兑换成功！消耗 {p} 学分",
                reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]])
            )
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text=f"📝 购买记录\n商品：{n}\n消耗：{p} 学分\n余额：{new_balance:.2f} 学分"
                )
            except Exception:
                pass
        else:
            await query.edit_message_text(
                "❌ 兑换失败，请稍后再试",
                reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]])
            )

# ========== 管理员命令 ==========
async def admin_credit_handler(update, ctx):
    if update.effective_chat.type not in ('group', 'supergroup'):
        return
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复你要操作的用户的消息，然后发送 /学分 +数字 或 /学分 -数字")
        return
    target = update.message.reply_to_message.from_user
    target_id = target.id
    target_name = target.first_name

    text = update.message.text.strip()
    match = re.match(r'^/学分\s+([+-]?\d+(?:\.\d+)?)', text)
    if not match:
        await update.message.reply_text("格式错误，请使用：/学分 +数字 或 /学分 -数字 (数字可为小数)")
        return
    delta_str = match.group(1)
    try:
        delta = float(delta_str)
    except ValueError:
        await update.message.reply_text("数字格式无效")
        return

    get_user(target_id, target_name)
    add_coins(target_id, delta, reason=f"管理员 {user_id} 操作")
    new_balance = get_coins(target_id)
    await update.message.reply_text(
        f"✅ 已为 {target_name}  {'增加' if delta > 0 else '扣除'} {abs(delta):.2f} 学分\n"
        f"📚 当前余额：{new_balance:.2f} 学分"
    )

# ========== 普通命令 ==========
async def cmd_start(update, ctx):
    if update.message.from_user.is_bot:
        return
    if update.effective_chat.type in ('group','supergroup') and update.effective_chat.id in ALLOWED_GROUPS:
        uid = update.effective_user.id
        name = update.effective_user.first_name
        get_user(uid, name)
        bal = get_coins(uid)
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        text = f"我的学分\n用户：{link}\n学分：{bal:.2f}"
        await update.message.reply_text(text, reply_markup=wallet_kb(), parse_mode=ParseMode.HTML)

async def cmd_coins(update, ctx):
    if update.message.from_user.is_bot:
        return
    if update.effective_chat.type in ('group','supergroup') and update.effective_chat.id in ALLOWED_GROUPS:
        uid = update.effective_user.id
        name = update.effective_user.first_name
        bal = get_coins(uid)
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        await update.message.reply_text(f"💰 {link}，你有 {bal:.2f} 学分。", parse_mode=ParseMode.HTML)

async def cmd_shop(update, ctx):
    if update.message.from_user.is_bot:
        return
    if update.effective_chat.type in ('group','supergroup') and update.effective_chat.id in ALLOWED_GROUPS:
        uid = update.effective_user.id
        name = update.effective_user.first_name
        get_user(uid, name)
        bal = get_coins(uid)
        await update.message.reply_text(
            f"🛒 学分商城\n💎 当前余额：{bal:.2f} 学分\n点击下方按钮兑换商品：",
            reply_markup=shop_kb()
        )

# ========== 启动 ==========
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("coins", cmd_coins))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.Regex(r'^/学分'), admin_credit_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    print("机器人已启动 | 动态掉落概率 | 首条3倍 | 无每日上限 | 管理员可修改学分 | 琪琪半价券全局限量1张")
    app.run_polling()

if __name__ == "__main__":
    main()
