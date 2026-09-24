import os
import random
import sqlite3
import re
import asyncio
import types
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler

# ========== 中国时区 ==========
CHINA_TZ = timezone(timedelta(hours=8))
def now_cn():
    return datetime.now(CHINA_TZ).replace(tzinfo=None)

# ========== 配置 ==========
TOKEN = "8179579064:AAHIJDWYkDDQhYJcltjvcgkh7VAFD11gH84"   # 请替换为你的Token
ALLOWED_GROUPS = [-1003002241602, -1003745425265, -1003720878201]  # 替换为你的群组ID
ADMIN_IDS = [8354445328, 877039616, 42438298]  # 替换为管理员ID

BASE_DROP_PROB = 0.14
TRIPLE_MULTIPLIER = 3
DB_PATH = "/data/credits.db"

SHOP = [
    (1, "100元福利券", 500),
    (2, "3个月TG会员", 1000),
    (3, "琪琪半价券", 500),
]

INTERVALS = [
    (0.60, 0.01, 0.3),
    (0.185, 0.3, 0.6),
    (0.05, 0.6, 0.8),
    (0.05, 0.8, 1.0),
    (0.05, 1.0, 2.0),
    (0.05, 2.0, 4.0),
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

def get_dynamic_drop_prob(today_gain):
    if today_gain >= 20:
        return 0.02
    elif today_gain >= 17:
        return 0.055
    elif today_gain >= 15:
        return 0.065
    elif today_gain >= 10:
        return 0.11
    else:
        return BASE_DROP_PROB

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
        c.execute("CREATE TABLE IF NOT EXISTS dice_rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, start_time TIMESTAMP, end_time TIMESTAMP, numbers TEXT, total INT, result TEXT, total_bets INT)")
        c.execute("CREATE TABLE IF NOT EXISTS dice_bets (round_id INT, user_id INT, amount REAL, bet_type TEXT, win REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS dice_state (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR IGNORE INTO dice_state (key, value) VALUES ('current_round', '0')")
        c.execute("INSERT OR IGNORE INTO dice_state (key, value) VALUES ('end_time', '')")
        c.execute("CREATE TABLE IF NOT EXISTS global_limits (item_id INT PRIMARY KEY)")
        c.execute("PRAGMA table_info(global_limits)")
        cols = [col[1] for col in c.fetchall()]
        if "remaining" not in cols:
            c.execute("ALTER TABLE global_limits ADD COLUMN remaining INT DEFAULT 0")
        c.execute("INSERT OR IGNORE INTO global_limits (item_id, remaining) VALUES (3, 1)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS lotteries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                prize TEXT,
                cost REAL,
                draw_time TIMESTAMP,
                status INTEGER DEFAULT 0,
                winner_id INTEGER,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                channel_id TEXT,
                need_msgs INTEGER DEFAULT 0,
                msg_count INTEGER DEFAULT 0,
                winners TEXT DEFAULT NULL
            )
        """)
        c.execute("PRAGMA table_info(lotteries)")
        existing_cols = [col[1] for col in c.fetchall()]
        if "channel_id" not in existing_cols:
            c.execute("ALTER TABLE lotteries ADD COLUMN channel_id TEXT")
        if "need_msgs" not in existing_cols:
            c.execute("ALTER TABLE lotteries ADD COLUMN need_msgs INTEGER DEFAULT 0")
        if "msg_count" not in existing_cols:
            c.execute("ALTER TABLE lotteries ADD COLUMN msg_count INTEGER DEFAULT 0")
        if "winners" not in existing_cols:
            c.execute("ALTER TABLE lotteries ADD COLUMN winners TEXT DEFAULT NULL")

        c.execute("""
            CREATE TABLE IF NOT EXISTS lottery_participants (
                lottery_id INTEGER,
                user_id INTEGER,
                participated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (lottery_id, user_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS lottery_fail_notify (
                lottery_id INTEGER,
                user_id INTEGER,
                chat_id INTEGER,
                PRIMARY KEY (lottery_id, user_id, chat_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_welfare (
                user_id INTEGER,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)
        c.execute("PRAGMA table_info(dice_bets)")
        dice_cols = [col[1] for col in c.fetchall()]
        if "chat_id" not in dice_cols:
            c.execute("ALTER TABLE dice_bets ADD COLUMN chat_id INTEGER DEFAULT 0")
        conn.commit()

# ---------- 用户函数 ----------
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
    today = now_cn().strftime('%Y-%m-%d')
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT gain FROM daily WHERE user_id=? AND date=?", (uid, today))
        row = c.fetchone()
        return row[0] if row else 0.0

def add_today_gain(uid, amt):
    today = now_cn().strftime('%Y-%m-%d')
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
                  (uid, "收入" if amt > 0 else "支出", abs(amt), reason, now_cn()))
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
                  (uid, "支出", amt, reason, now_cn()))
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
    today = now_cn().strftime('%Y-%m-%d')
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT used FROM daily_first_bonus WHERE user_id=? AND date=?", (uid, today))
        row = c.fetchone()
        if row and row[0] == 1:
            return False
        c.execute("INSERT OR REPLACE INTO daily_first_bonus (user_id, date, used) VALUES (?,?,1)", (uid, today))
        conn.commit()
        return True

# ---------- 限量商品通用 ----------
def get_remaining(item_id):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT remaining FROM global_limits WHERE item_id=?", (item_id,))
        row = c.fetchone()
        return row[0] if row else None

def decrease_remaining(item_id):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE global_limits SET remaining = remaining - 1 WHERE item_id=? AND remaining > 0", (item_id,))
        conn.commit()
        return c.rowcount > 0

# ---------- 低保函数 ----------
def get_welfare_today_count(uid):
    today = now_cn().strftime('%Y-%m-%d')
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT count FROM daily_welfare WHERE user_id=? AND date=?", (uid, today))
        row = c.fetchone()
        return row[0] if row else 0

def add_welfare_record(uid):
    today = now_cn().strftime('%Y-%m-%d')
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO daily_welfare (user_id, date, count) VALUES (?,?,1) ON CONFLICT(user_id,date) DO UPDATE SET count = count + 1",
                  (uid, today))
        conn.commit()

# ========== 键盘 ==========
def wallet_kb():
    return Markup([
        [Btn("🛒 兑换商品", callback_data="shop"),
         Btn("📚 学分记录", callback_data="history")]
    ])

def shop_kb():
    keyboard = []
    for i, n, p in SHOP:
        rem = get_remaining(i)
        if rem is None:
            text = f"{n} - {p}💎"
        elif rem > 0:
            text = f"{n} 剩余{rem} - {p}💎"
        else:
            text = f"{n} 已售罄 - {p}💎"
        keyboard.append([Btn(text, callback_data=f"buy_{i}")])
    return Markup(keyboard)

# ========== 回调处理器 ==========
async def cb(update, ctx):
    if update.callback_query.from_user.is_bot:
        return
    if update.effective_chat.type in ('group', 'supergroup'):
        if update.effective_chat.id not in ALLOWED_GROUPS:
            await update.callback_query.answer("该群组未授权使用本机器人。")
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
        remaining = get_remaining(iid)
        if remaining is not None and remaining <= 0:
            await query.edit_message_text(
                "❌ 商品已换完，下次早点来哦！",
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
            if remaining is not None:
                decrease_remaining(iid)
            new_balance = get_coins(uid)
            await query.edit_message_text(
                f"✅ {n} 兑换成功！消耗 {p} 学分",
                reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]])
            )
            if update.effective_chat.type in ('group', 'supergroup'):
                try:
                    msg = f"🎉 {name} 成功兑换了 {n}！消耗 {p} 学分。"
                    await ctx.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=msg,
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    print(f"群组通知发送失败: {e}")
            for aid in ADMIN_IDS:
                try:
                    admin_msg = f"用户 {name}（ID: {uid}）兑换了 {n}，消耗 {p} 学分。"
                    await ctx.bot.send_message(
                        chat_id=aid,
                        text=admin_msg
                    )
                except Exception as e:
                    print(f"私聊管理员 {aid} 失败: {e}")
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

    # ---------- 抽奖参与回调 ----------
    elif data.startswith("lottery_join_"):
        lottery_id = int(data.split("_")[2])
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT title, prize, cost, status, draw_time, channel_id, need_msgs, msg_count FROM lotteries WHERE id=?", (lottery_id,))
            row = c.fetchone()
            if not row:
                await query.answer("❌ 抽奖不存在", show_alert=True)
                return
            title, prize, cost, status, draw_time, channel_id, need_msgs, msg_count = row
            if isinstance(draw_time, str):
                draw_time = datetime.fromisoformat(draw_time)
            if status != 0:
                await query.answer("❌ 该抽奖已结束或已开奖", show_alert=True)
                return
            if now_cn() > draw_time:
                await query.answer("❌ 该抽奖已过开奖时间", show_alert=True)
                return

            if need_msgs > 0 and msg_count < need_msgs:
                await query.answer(f"❌ 群内发言数未达标（{msg_count}/{need_msgs}），暂无法参与", show_alert=True)
                return

            c.execute("SELECT 1 FROM lottery_participants WHERE lottery_id=? AND user_id=?", (lottery_id, uid))
            if c.fetchone():
                await query.answer("您已参与过本次抽奖", show_alert=True)
                return

            if channel_id:
                try:
                    member = await ctx.bot.get_chat_member(chat_id=channel_id, user_id=uid)
                    if member.status not in ['member', 'administrator', 'creator']:
                        if query.message.chat.type in ('group', 'supergroup'):
                            chat_id = query.message.chat.id
                            with db_connect() as conn2:
                                c2 = conn2.cursor()
                                c2.execute("SELECT 1 FROM lottery_fail_notify WHERE lottery_id=? AND user_id=? AND chat_id=?",
                                           (lottery_id, uid, chat_id))
                                if not c2.fetchone():
                                    try:
                                        mention = f'<a href="tg://user?id={uid}">{name}</a>'
                                        await ctx.bot.send_message(
                                            chat_id=chat_id,
                                            text=f"⚠️ {mention} 参与抽奖「{title}」需要先关注频道 {channel_id}，请关注后再次尝试。",
                                            parse_mode=ParseMode.HTML
                                        )
                                        c2.execute("INSERT INTO lottery_fail_notify (lottery_id, user_id, chat_id) VALUES (?,?,?)",
                                                   (lottery_id, uid, chat_id))
                                        conn2.commit()
                                    except Exception as e:
                                        print(f"发送群聊提示失败: {e}")
                        await query.answer(f"❌ 请先关注频道 {channel_id} 再来参与", show_alert=True)
                        return
                except Exception as e:
                    await query.answer(f"❌ 频道校验失败，请稍后重试", show_alert=True)
                    print(f"频道校验异常: {e}")
                    return

            bal = get_coins(uid)
            if bal < cost:
                await query.answer(f"❌ 学分不足，需要 {cost} 学分", show_alert=True)
                return
            if not sub_coins(uid, cost, f"参与抽奖 {title} - {prize}"):
                await query.answer("❌ 扣学分失败", show_alert=True)
                return

            c.execute("INSERT INTO lottery_participants (lottery_id, user_id) VALUES (?, ?)", (lottery_id, uid))
            conn.commit()

        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"✅ 你已成功参与抽奖「{title}」！\n奖品：{prize}\n消耗：{cost} 学分\n开奖时间：{draw_time}\n请等待开奖结果。"
            )
        except Exception:
            pass

        with db_connect() as conn2:
            c2 = conn2.cursor()
            c2.execute("SELECT user_id FROM lottery_participants WHERE lottery_id=?", (lottery_id,))
            participants = [p[0] for p in c2.fetchall()]
            names = []
            for pid in participants:
                c3 = conn2.cursor()
                c3.execute("SELECT nickname FROM users WHERE user_id=?", (pid,))
                nick = c3.fetchone()
                names.append(nick[0] if nick else str(pid))

        expired = now_cn() > draw_time
        already = True
        display_prize = prize.replace(',', '、').replace('，', '、')
        msg_text = f"🎰 当前抽奖活动\n标题：{title}\n奖品：{display_prize}\n消耗：{cost} 学分\n开奖时间：{draw_time}\n"
        if channel_id:
            msg_text += f"📢 参与条件：需关注频道 {channel_id}\n"
        if need_msgs > 0:
            msg_text += f"💬 需群内发言数 ≥ {need_msgs} 条（当前 {msg_count} 条）\n"
        if names:
            msg_text += f"👥 已参与（{len(names)}人）：{', '.join(names)}\n"
        else:
            msg_text += "👥 暂无人参与\n"
        if expired:
            msg_text += "⏰ 该抽奖已过开奖时间，无法参与。"
        elif already:
            msg_text += "你已参与，请等待开奖。"
        else:
            msg_text += "点击下方按钮参与！"

        original_markup = query.message.reply_markup
        try:
            await query.edit_message_text(msg_text, reply_markup=original_markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"编辑抽奖消息失败: {e}")

        await query.answer("✅ 参与成功！", show_alert=False)

# ========== 测试回调 ==========
async def test_callback(update, ctx):
    await update.message.reply_text(
        "测试按钮：",
        reply_markup=Markup([[Btn("点击测试", callback_data="test")]])
    )

async def test_cb(update, ctx):
    print(">>> 测试回调被触发！")
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ 回调测试成功！")

# ========== 管理员命令 ==========
async def admin_credit_handler(update, ctx):
    if update.effective_chat.type not in ('group', 'supergroup'):
        return
    if update.effective_chat.id not in ALLOWED_GROUPS:
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

async def admin_add_item(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    args = ctx.args
    if len(args) != 3:
        await update.message.reply_text("用法：/additem <商品名称> <价格> <限量（0为无限）>\n例如：/additem 测试商品 100 5")
        return
    name = args[0]
    try:
        price = float(args[1])
        if price <= 0:
            raise ValueError
        limit_total = int(args[2])
        if limit_total < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("价格必须是正数，限量必须是非负整数。")
        return
    new_id = len(SHOP) + 1
    SHOP.append((new_id, name, price))
    if limit_total > 0:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO global_limits (item_id, remaining) VALUES (?, ?)", (new_id, limit_total))
            conn.commit()
    await update.message.reply_text(f"✅ 商品「{name}」已上架，ID={new_id}，价格={price}，限量={limit_total if limit_total>0 else '无限'}")

async def admin_list_items(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    if not SHOP:
        await update.message.reply_text("暂无商品。")
        return
    text = "📦 商品列表：\n"
    for gid, name, price in SHOP:
        rem = get_remaining(gid)
        if rem is None:
            text += f"ID:{gid} {name} - {price}💎 (无限量)\n"
        else:
            text += f"ID:{gid} {name} - {price}💎 (剩余{rem})\n"
    await update.message.reply_text(text)

async def admin_del_item(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    args = ctx.args
    if len(args) != 1:
        await update.message.reply_text("用法：/delitem <商品ID>")
        return
    try:
        gid = int(args[0])
    except ValueError:
        await update.message.reply_text("商品ID必须是数字。")
        return
    global SHOP
    new_shop = [item for item in SHOP if item[0] != gid]
    if len(new_shop) == len(SHOP):
        await update.message.reply_text(f"❌ 商品ID {gid} 不存在。")
    else:
        SHOP = new_shop
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM global_limits WHERE item_id = ?", (gid,))
            conn.commit()
        await update.message.reply_text(f"✅ 商品ID {gid} 已删除。")

# ========== 抽奖管理员命令 ==========
async def cmd_create_lottery(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    text = update.message.text.strip()
    if text.startswith('/cj'):
        content = text[3:].strip()
    else:
        content = text

    channel_id = None
    need_msgs = 0
    parts = content.split()
    if '-c' in parts:
        idx = parts.index('-c')
        if idx + 1 < len(parts):
            channel_id = parts[idx + 1]
            parts.pop(idx)
            parts.pop(idx)
    if '-f' in parts:
        idx = parts.index('-f')
        if idx + 1 < len(parts):
            try:
                need_msgs = int(parts[idx + 1])
                if need_msgs < 0:
                    need_msgs = 0
            except ValueError:
                need_msgs = 0
            parts.pop(idx)
            parts.pop(idx)
    content = ' '.join(parts)

    time_pattern = r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})'
    match = re.search(time_pattern, content)
    if not match:
        await update.message.reply_text(
            "未找到有效时间，请使用格式：YYYY-MM-DD HH:MM\n"
            "示例：/cj 8月活动 商品1,商品2 10 2026-08-01 20:00 -c @channel -f 80"
        )
        return
    draw_time_str = match.group(1)
    try:
        dt = datetime.strptime(draw_time_str, "%Y-%m-%d %H:%M")
        now = now_cn()
        if dt <= now:
            await update.message.reply_text(f"开奖时间必须在未来。当前时间：{now.strftime('%Y-%m-%d %H:%M')}")
            return
        if (dt - now).total_seconds() < 60:
            await update.message.reply_text(f"⚠️ 开奖时间与当前时间相差小于1分钟，建议设置至少1分钟后的时间。")
        draw_time = dt
    except ValueError:
        await update.message.reply_text("时间格式无效，请使用 YYYY-MM-DD HH:MM")
        return

    rest = content.replace(draw_time_str, '').strip()
    parts = rest.split()
    if len(parts) < 2:
        await update.message.reply_text("格式错误，请提供：标题、奖品、消耗学分")
        return
    try:
        cost = float(parts[-1])
        if cost <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("消耗学分必须是正数。")
        return
    title = parts[0]
    if len(parts) > 2:
        prize_raw = ' '.join(parts[1:-1])
    else:
        prize_raw = "未命名奖品"
    prize = prize_raw

    if channel_id:
        try:
            chat = await ctx.bot.get_chat(channel_id)
            try:
                me = await ctx.bot.get_me()
                member = await ctx.bot.get_chat_member(chat_id=channel_id, user_id=me.id)
                if member.status not in ['administrator', 'creator']:
                    await update.message.reply_text(
                        f"⚠️ 机器人在频道 {channel_id} 中，但不是管理员，无法校验成员关注状态。\n"
                        f"请将机器人设为管理员后再试。"
                    )
                    return
            except Exception as e:
                await update.message.reply_text(
                    f"❌ 机器人不在频道 {channel_id} 中，或无法获取频道信息。\n"
                    f"请先将机器人加入该频道并设为管理员，然后重新创建抽奖。\n"
                    f"错误详情: {e}"
                )
                return
        except Exception as e:
            await update.message.reply_text(
                f"❌ 无法访问频道 {channel_id}，请确认频道存在且机器人已加入。\n"
                f"错误详情: {e}"
            )
            return

    with db_connect() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO lotteries (title, prize, cost, draw_time, status, created_by, channel_id, need_msgs, msg_count) VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0)",
            (title, prize, cost, draw_time, update.effective_user.id, channel_id, need_msgs)
        )
        lid = c.lastrowid
        conn.commit()

    display_prize = prize.replace(',', '、').replace('，', '、')
    msg = f"✅ 抽奖已创建！ID: {lid}\n标题：{title}\n奖品：{display_prize}\n消耗：{cost} 学分\n开奖时间：{draw_time.strftime('%Y-%m-%d %H:%M')}\n"
    if channel_id:
        msg += f"📢 参与条件：需关注频道 {channel_id}\n"
    if need_msgs > 0:
        msg += f"💬 需群内发言数 ≥ {need_msgs} 条（自创建起统计）\n"
    msg += f"⏰ 当前服务器时间：{now_cn().strftime('%Y-%m-%d %H:%M')}"
    await update.message.reply_text(msg)

async def cmd_list_lotteries(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, prize, cost, draw_time, status, winner_id, channel_id, need_msgs, msg_count, winners FROM lotteries ORDER BY id DESC")
        rows = c.fetchall()
    if not rows:
        await update.message.reply_text("暂无抽奖记录。")
        return
    status_map = {0: "⏳ 未开始", 1: "🔚 已结束", 2: "🏆 已开奖"}
    text = "📋 抽奖列表：\n"
    for row in rows:
        lid, title, prize, cost, dt, status, winner, channel, need_msgs, msg_count, winners = row
        status_str = status_map.get(status, "未知")
        display_prize = prize.replace(',', '、').replace('，', '、')
        text += f"ID:{lid} | {title} | {display_prize} | 消耗{cost} | {dt} | {status_str}"
        if channel:
            text += f" | 频道:{channel}"
        if need_msgs > 0:
            text += f" | 需发言≥{need_msgs} (当前{msg_count})"
        if winners:
            winner_ids = [int(x) for x in winners.split(',')
