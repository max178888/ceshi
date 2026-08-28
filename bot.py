import os
import random
import sqlite3
import re
import asyncio
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler

# ========== 中国时区 ==========
CHINA_TZ = timezone(timedelta(hours=8))
def now_cn():
    return datetime.now(CHINA_TZ).replace(tzinfo=None)

# ========== 配置 ==========
TOKEN = "8179579064:AAF57RUAH5TVtrW4qdA4_wIAtWkRAAkqkvo"   # 请替换为你的Token
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
        confirm_kb = Markup([
            [Btn("✅ 确认兑换", callback_data=f"confirm_buy_{iid}")],
            [Btn("🔙 取消", callback_data="back_to_shop")]
        ])
        await query.edit_message_text(
            f"❓ 确认兑换 {n}？\n消耗：{p} 学分\n当前余额：{current:.2f} 学分\n\n请确认是否兑换。",
            reply_markup=confirm_kb
        )
    elif data.startswith("confirm_buy_"):
        iid = int(data.split("_")[2])
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
            success_kb = Markup([[Btn("🔙 返回钱包", callback_data="back")]])
            await query.edit_message_text(
                f"✅ {n} 兑换成功！消耗 {p} 学分，当前余额 {new_balance:.2f} 学分。",
                reply_markup=success_kb
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
    elif data == "back_to_shop":
        bal = get_coins(uid)
        await query.edit_message_text(
            f"🛒 学分商城\n💎 当前余额：{bal:.2f} 学分\n点击下方按钮兑换商品：",
            reply_markup=shop_kb()
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
    # 仅限群聊，回复消息
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

# ========== 私聊管理员加学分（使用 ctx.args） ==========
async def admin_credit_private(update, ctx):
    """私聊处理 /credit @用户名 金额"""
    if update.effective_chat.type != 'private':
        return
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    args = ctx.args
    if len(args) != 2:
        await update.message.reply_text("格式错误，请使用：/credit @用户名 金额  或 /credit 用户ID 金额\n金额可带正负号，如 +100 或 -50")
        return
    target_identifier = args[0]
    delta_str = args[1]
    try:
        delta = float(delta_str)
    except ValueError:
        await update.message.reply_text("金额格式无效，请输入数字。")
        return

    # 解析目标用户ID
    target_uid = None
    target_name = None
    if target_identifier.startswith('@'):
        try:
            chat = await ctx.bot.get_chat(target_identifier)
            target_uid = chat.id
            target_name = chat.first_name or str(target_uid)
        except Exception:
            await update.message.reply_text(f"❌ 无法找到用户 {target_identifier}，请确认用户名正确或使用数字ID。")
            return
    else:
        try:
            target_uid = int(target_identifier)
            try:
                chat = await ctx.bot.get_chat(target_uid)
                target_name = chat.first_name or str(target_uid)
            except:
                target_name = str(target_uid)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字。")
            return

    get_user(target_uid, target_name)
    add_coins(target_uid, delta, reason=f"管理员 {user_id} 私聊操作")
    new_balance = get_coins(target_uid)
    action = "增加" if delta > 0 else "扣除"
    await update.message.reply_text(
        f"✅ 已为 {target_name} (ID: {target_uid}) {action} {abs(delta):.2f} 学分\n"
        f"📚 当前余额：{new_balance:.2f} 学分"
    )

# ========== 管理员商品管理 ==========
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
            winner_ids = [int(x) for x in winners.split(',') if x.strip().isdigit()]
            names = []
            for wid in winner_ids:
                with db_connect() as conn2:
                    c2 = conn2.cursor()
                    c2.execute("SELECT nickname FROM users WHERE user_id=?", (wid,))
                    w = c2.fetchone()
                    names.append(w[0] if w else str(wid))
            text += f" | 获奖者：{', '.join(names)}"
        elif winner:
            with db_connect() as conn2:
                c2 = conn2.cursor()
                c2.execute("SELECT nickname FROM users WHERE user_id=?", (winner,))
                w = c2.fetchone()
                winner_name = w[0] if w else str(winner)
            text += f" | 获奖者：{winner_name}"
        text += "\n"
    await update.message.reply_text(text)

# ========== /qx 取消抽奖 ==========
async def cmd_cancel_lottery(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    args = ctx.args
    if len(args) != 1:
        await update.message.reply_text("用法：/qx <抽奖ID>")
        return
    try:
        lid = int(args[0])
    except ValueError:
        await update.message.reply_text("抽奖ID必须是数字。")
        return

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, status FROM lotteries WHERE id=?", (lid,))
        row = c.fetchone()
        if not row:
            await update.message.reply_text("抽奖不存在。")
            return
        lid, title, status = row
        if status != 0:
            await update.message.reply_text("该抽奖已结束或已开奖，无法取消。")
            return
        c.execute("UPDATE lotteries SET status=1 WHERE id=?", (lid,))
        conn.commit()
    await update.message.reply_text(f"✅ 抽奖「{title}」（ID:{lid}）已取消，不产生获奖者。")

# ========== /gg 修改开奖时间 ==========
async def cmd_change_time(update, ctx):
    """修改开奖时间：/gg <抽奖ID> <新时间 YYYY-MM-DD HH:MM>"""
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    text = update.message.text.strip()
    if text.startswith('/gg'):
        content = text[3:].strip()
    else:
        content = text

    time_pattern = r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})'
    match = re.search(time_pattern, content)
    if not match:
        await update.message.reply_text("未找到有效时间，请使用格式：YYYY-MM-DD HH:MM\n例如：/gg 5 2026-08-18 10:00")
        return
    new_time_str = match.group(1)

    parts = content.split()
    if not parts:
        await update.message.reply_text("请提供抽奖ID。")
        return
    try:
        lid = int(parts[0])
    except ValueError:
        await update.message.reply_text("抽奖ID必须是数字。")
        return

    try:
        new_dt = datetime.strptime(new_time_str, "%Y-%m-%d %H:%M")
        if new_dt <= now_cn():
            await update.message.reply_text(f"新时间必须在未来。当前时间：{now_cn().strftime('%Y-%m-%d %H:%M')}")
            return
    except ValueError:
        await update.message.reply_text("时间格式无效，请使用 YYYY-MM-DD HH:MM")
        return

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, draw_time, status FROM lotteries WHERE id=?", (lid,))
        row = c.fetchone()
        if not row:
            await update.message.reply_text("抽奖不存在。")
            return
        lid, title, old_time, status = row
        if status != 0:
            await update.message.reply_text("该抽奖已结束或已开奖，无法修改时间。")
            return
        c.execute("UPDATE lotteries SET draw_time=? WHERE id=?", (new_dt, lid))
        conn.commit()
    await update.message.reply_text(
        f"✅ 抽奖「{title}」（ID:{lid}）的开奖时间已更新：\n"
        f"旧时间：{old_time}\n新时间：{new_dt.strftime('%Y-%m-%d %H:%M')}"
    )

# ========== /QL 清理抽奖数据 ==========
async def cmd_clean_lottery(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    args = ctx.args
    if len(args) != 1:
        await update.message.reply_text("用法：/QL <抽奖ID>")
        return
    try:
        lid = int(args[0])
    except ValueError:
        await update.message.reply_text("抽奖ID必须是数字。")
        return

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, status FROM lotteries WHERE id=?", (lid,))
        row = c.fetchone()
        if not row:
            await update.message.reply_text("抽奖不存在。")
            return
        lid, title, status = row
        c.execute("DELETE FROM lottery_participants WHERE lottery_id=?", (lid,))
        c.execute("UPDATE lotteries SET status=0, winner_id=NULL, winners=NULL, msg_count=0 WHERE id=?", (lid,))
        conn.commit()
    await update.message.reply_text(f"✅ 抽奖「{title}」（ID:{lid}）已重置：参与者、获奖者、发言统计已清空，状态恢复为未开始。")

# ========== /sb 取消用户参与（不退还学分） ==========
async def cmd_remove_user_lottery(update, ctx):
    if update.effective_chat.type != 'private':
        return
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 只有管理员可以使用此命令。")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("用法：/sb <用户ID> 或 /sb @用户名\n例如：/sb 123456789 或 /sb @someone")
        return
    user_identifier = args[0]
    target_uid = None
    if user_identifier.isdigit():
        target_uid = int(user_identifier)
    elif user_identifier.startswith('@'):
        try:
            chat = await ctx.bot.get_chat(user_identifier)
            target_uid = chat.id
        except Exception:
            await update.message.reply_text("❌ 无法通过 @用户名 获取用户ID，请直接输入数字ID。")
            return
    else:
        await update.message.reply_text("❌ 请输入有效的用户ID（数字）或 @用户名。")
        return

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT l.id, l.title
            FROM lottery_participants lp
            JOIN lotteries l ON lp.lottery_id = l.id
            WHERE lp.user_id = ? AND l.status = 0
        """, (target_uid,))
        rows = c.fetchall()
        if not rows:
            await update.message.reply_text(f"用户 {target_uid} 没有参与任何进行中的抽奖。")
            return

        titles = []
        for lid, title in rows:
            c.execute("DELETE FROM lottery_participants WHERE lottery_id=? AND user_id=?", (lid, target_uid))
            titles.append(title)
        conn.commit()

    user_name = str(target_uid)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nickname FROM users WHERE user_id=?", (target_uid,))
        row = c.fetchone()
        if row:
            user_name = row[0]

    await update.message.reply_text(
        f"✅ 已取消用户 {user_name}（ID:{target_uid}）在以下抽奖中的参与资格（不退还学分）：\n"
        f"{', '.join(titles)}"
    )

# ========== 抽奖核心开奖函数 ==========
async def do_draw(lottery_id, bot, force=False):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, prize, cost, status, draw_time, channel_id, need_msgs, msg_count FROM lotteries WHERE id=?", (lottery_id,))
        row = c.fetchone()
        if not row:
            return False, "抽奖不存在"
        lid, title, prize, cost, status, draw_time, channel, need_msgs, msg_count = row
        if status != 0:
            return False, "该抽奖已结束或已开奖"

        if need_msgs > 0 and msg_count < need_msgs:
            return False, f"发言数未达标 ({msg_count}/{need_msgs})，暂不开奖。"

        c.execute("SELECT user_id FROM lottery_participants WHERE lottery_id=?", (lid,))
        participants = [r[0] for r in c.fetchall()]
        if not participants:
            if force:
                c.execute("UPDATE lotteries SET status=1 WHERE id=?", (lid,))
                conn.commit()
                return False, "该抽奖暂无参与者，已强制结束。"
            else:
                return False, "暂无参与者，未开奖，等待管理员处理。"

        prize_list = [p.strip() for p in re.split(r'[,，]', prize) if p.strip()]
        if not prize_list:
            prize_list = ["未命名奖品"]

        shuffled = participants.copy()
        random.shuffle(shuffled)
        winner_count = min(len(prize_list), len(shuffled))
        winners = shuffled[:winner_count]

        first_winner = winners[0] if winners else None
        winners_str = ','.join(str(uid) for uid in winners) if winners else ''
        c.execute("UPDATE lotteries SET status=2, winner_id=?, winners=? WHERE id=?", (first_winner, winners_str, lid))
        conn.commit()

        winner_links = []
        for uid in winners:
            c2 = conn.cursor()
            c2.execute("SELECT nickname FROM users WHERE user_id=?", (uid,))
            wrow = c2.fetchone()
            name = wrow[0] if wrow else str(uid)
            winner_links.append(f'<a href="tg://user?id={uid}">{name}</a>')

        msg = f"🎉 抽奖开奖结果！\n标题：{title}\n\n"
        for i, (prize_name, link) in enumerate(zip(prize_list[:winner_count], winner_links)):
            msg += f"🏆 奖品：{prize_name} → 获奖者：{link}\n"
        if len(prize_list) > winner_count:
            msg += f"\n⚠️ 参与者数量不足，剩余 {len(prize_list)-winner_count} 个奖品无人获得。"
        elif len(participants) > len(prize_list):
            msg += f"\n🎉 恭喜所有获奖者！"
        else:
            msg += f"\n🎉 恭喜所有获奖者！"
    for gid in ALLOWED_GROUPS:
        try:
            await bot.send_message(chat_id=gid, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"发送开奖结果到 {gid} 失败: {e}")
    return True, f"抽奖 {lid} 已开奖，产生 {len(winners)} 位获奖者。"

# ========== 后台自动开奖任务 ==========
async def auto_draw_loop(bot):
    while True:
        try:
            now = now_cn()
            with db_connect() as conn:
                c = conn.cursor()
                c.execute("SELECT id, draw_time FROM lotteries WHERE status=0")
                rows = c.fetchall()
            for lid, draw_time_str in rows:
                try:
                    draw_time = datetime.fromisoformat(draw_time_str)
                except:
                    continue
                if draw_time <= now and (now - draw_time).total_seconds() <= 120:
                    success, msg = await do_draw(lid, bot, force=False)
                    if success:
                        print(f"自动开奖 {lid}: {msg}")
                    else:
                        print(f"自动开奖 {lid}: {msg}")
                elif draw_time <= now:
                    pass
        except Exception as e:
            print(f"自动开奖循环出错: {e}")
        await asyncio.sleep(60)

# ========== 普通命令 ==========
async def cmd_start(update, ctx):
    if update.message.from_user.is_bot:
        return
    if update.effective_chat.type == 'private':
        uid = update.effective_user.id
        if uid in ADMIN_IDS:
            help_text = (
                "🤖 管理员指令：\n"
                "/additem <名称> <价格> <限量> - 添加商品（限量0为无限）\n"
                "/listitems - 查看商品列表\n"
                "/delitem <商品ID> - 删除商品\n"
                "群聊：/学分 +数字 或 /学分 -数字（需回复用户消息）\n"
                "私聊：/credit @用户名 金额  或 /credit 用户ID 金额（可带正负号）\n"
                "/coins - 查询自己学分\n"
                "/shop - 打开商城\n"
                "/start - 显示本帮助\n"
                "\n🎰 抽奖管理（私聊）：\n"
                "/cj <标题> <奖品1,奖品2,...> <消耗学分> <开奖时间> [-c @channel] [-f 发言数] - 创建抽奖\n"
                "/cjlist - 查看所有抽奖\n"
                "/qx <抽奖ID> - 取消抽奖\n"
                "/gg <抽奖ID> <新时间> - 修改开奖时间\n"
                "/QL <抽奖ID> - 重置抽奖（清空参与者和获奖者）\n"
                "/sb <用户ID/@用户名> - 取消某人在所有进行中抽奖的参与资格（不退还学分）\n"
            )
            await update.message.reply_text(help_text)
        else:
            return
        return
    if update.effective_chat.type in ('group', 'supergroup'):
        if update.effective_chat.id not in ALLOWED_GROUPS:
            return
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
    if update.effective_chat.type in ('group', 'supergroup'):
        if update.effective_chat.id not in ALLOWED_GROUPS:
            return
    uid = update.effective_user.id
    name = update.effective_user.first_name
    bal = get_coins(uid)
    link = f'<a href="tg://user?id={uid}">{name}</a>'
    await update.message.reply_text(f"💰 {link}，你有 {bal:.2f} 学分。", parse_mode=ParseMode.HTML)

async def cmd_shop(update, ctx):
    if update.message.from_user.is_bot:
        return
    if update.effective_chat.type in ('group', 'supergroup'):
        if update.effective_chat.id not in ALLOWED_GROUPS:
            return
    uid = update.effective_user.id
    name = update.effective_user.first_name
    get_user(uid, name)
    bal = get_coins(uid)
    await update.message.reply_text(
        f"🛒 学分商城\n💎 当前余额：{bal:.2f} 学分\n点击下方按钮兑换商品：",
        reply_markup=shop_kb()
    )

# ========== 消息处理器 ==========
async def on_msg(update, ctx):
    if update.message.from_user.is_bot:
        return
    if not update.message or not update.message.text:
        return
    if update.effective_chat.type in ('group', 'supergroup'):
        if update.effective_chat.id not in ALLOWED_GROUPS:
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
    if text == "学分":
        uid = update.message.from_user.id
        name = update.message.from_user.first_name
        bal = get_coins(uid)
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        await update.message.reply_text(f"💰 {link}，你的当前余额是 {bal:.2f} 学分。", parse_mode=ParseMode.HTML)
        return
    if text == "排行榜":
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, nickname, coins FROM users ORDER BY coins DESC LIMIT 50")
            rows = c.fetchall()
        if not rows:
            await update.message.reply_text("暂无用户数据。")
            return
        msg = "🏆 学分排行榜 (前50)\n"
        for idx, (uid, nick, coins) in enumerate(rows, 1):
            name = nick if nick else f"用户{uid}"
            msg += f"{idx}. {name}: {coins:.2f}学分\n"
        await update.message.reply_text(msg)
        return

    # ========== 查看近2天开奖记录 ==========
    if text == "开奖":
        now = now_cn()
        two_days_ago = now - timedelta(days=2)
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, title, prize, draw_time, winners
                FROM lotteries
                WHERE status = 2 AND draw_time >= ?
                ORDER BY draw_time DESC
                LIMIT 20
            """, (two_days_ago,))
            rows = c.fetchall()
        if not rows:
            await update.message.reply_text("📭 近2天内暂无开奖记录。")
            return
        msg = "📋 近2天开奖记录：\n\n"
        for idx, (lid, title, prize, draw_time, winners_str) in enumerate(rows, 1):
            prize_list = [p.strip() for p in re.split(r'[,，]', prize) if p.strip()]
            if not prize_list:
                prize_list = ["未命名奖品"]
            winner_ids = []
            if winners_str:
                winner_ids = [int(x) for x in winners_str.split(',') if x.strip().isdigit()]
            items = []
            for i, p in enumerate(prize_list):
                if i < len(winner_ids):
                    wid = winner_ids[i]
                    with db_connect() as conn2:
                        c2 = conn2.cursor()
                        c2.execute("SELECT nickname FROM users WHERE user_id=?", (wid,))
                        wrow = c2.fetchone()
                        name = wrow[0] if wrow else str(wid)
                    winner_link = f'<a href="tg://user?id={wid}">{name}</a>'
                    items.append(f"{p} → {winner_link}")
                else:
                    items.append(f"{p} → ❌ 无人获得")
            msg += f"{idx}. 🎯 标题：{title}\n"
            msg += f"   🕒 开奖时间：{draw_time}\n"
            for item in items:
                msg += f"   🎁 {item}\n"
            msg += "\n"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    # ========== 低保命令（检查是否有进行中的骰子投注） ==========
    if text == "低保":
        uid = update.message.from_user.id
        name = update.message.from_user.first_name
        get_user(uid, name)
        bal = get_coins(uid)

        # 检查是否有进行中的骰子投注
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM dice_state WHERE key='current_round'")
            row = c.fetchone()
            if row:
                current_round = int(row[0])
                if current_round > 0:
                    c.execute("SELECT 1 FROM dice_bets WHERE round_id=? AND user_id=? AND win IS NULL", (current_round, uid))
                    if c.fetchone():
                        await update.message.reply_text("您有进行中的骰子投注，无法领取低保，请等待开奖结果后再试。")
                        return

        if bal >= 5:
            await update.message.reply_text("您的学分已达到或超过 5 分，无需领取低保。")
            return
        today_count = get_welfare_today_count(uid)
        if today_count >= 10:
            await update.message.reply_text("您今天已领取 10 次低保，已达上限，请明天再试。")
            return
        add_coins(uid, 5, "每日低保领取")
        add_welfare_record(uid)
        new_bal = get_coins(uid)
        remaining = 10 - (today_count + 1)
        await update.message.reply_text(
            f"✅ 低保发放成功！\n"
            f"您当前余额：{new_bal:.2f} 学分\n"
            f"今日剩余领取次数：{remaining} 次"
        )
        return

    # ========== 抽奖参与 ==========
    if text == "抽奖":
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT id, title, prize, cost, draw_time, channel_id, need_msgs, msg_count FROM lotteries WHERE status=0 ORDER BY draw_time ASC")
            rows = c.fetchall()
        if not rows:
            await update.message.reply_text("当前没有任何抽奖活动，请关注后续通知。")
            return

        for lid, title, prize, cost, draw_time, channel_id, need_msgs, msg_count in rows:
            if isinstance(draw_time, str):
                draw_time = datetime.fromisoformat(draw_time)
            expired = now_cn() > draw_time
            with db_connect() as conn2:
                c2 = conn2.cursor()
                c2.execute("SELECT 1 FROM lottery_participants WHERE lottery_id=? AND user_id=?", (lid, update.effective_user.id))
                already = c2.fetchone() is not None
                c2.execute("SELECT user_id FROM lottery_participants WHERE lottery_id=?", (lid,))
                participants = [p[0] for p in c2.fetchall()]
                names = []
                for pid in participants:
                    c3 = conn2.cursor()
                    c3.execute("SELECT nickname FROM users WHERE user_id=?", (pid,))
                    nick = c3.fetchone()
                    names.append(nick[0] if nick else str(pid))
            btn = Btn("🎟️ 参与抽奖", callback_data=f"lottery_join_{lid}") if not expired else None
            kb = Markup([[btn]]) if btn else None
            display_prize = prize.replace(',', '、').replace('，', '、')
            msg = f"🎰 当前抽奖活动\n标题：{title}\n奖品：{display_prize}\n消耗：{cost} 学分\n开奖时间：{draw_time}\n"
            if channel_id:
                msg += f"📢 参与条件：需关注频道 {channel_id}\n"
            if need_msgs > 0:
                msg += f"💬 需群内发言数 ≥ {need_msgs} 条（当前 {msg_count} 条）\n"
            if names:
                msg += f"👥 已参与（{len(names)}人）：{', '.join(names)}\n"
            else:
                msg += "👥 暂无人参与\n"
            if expired:
                msg += "⏰ 该抽奖已过开奖时间，无法参与。"
            else:
                msg += "点击下方按钮参与！"
            await update.message.reply_text(msg, reply_markup=kb)
        return

    if text.startswith('/'):
        return

    # ===== 发言统计 =====
    with db_connect() as conn:
        c = conn.cursor()
        now = now_cn()
        c.execute("SELECT id, created_at FROM lotteries WHERE status=0 AND need_msgs > 0")
        rows = c.fetchall()
    for lid, created_at in rows:
        try:
            created_dt = datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
        except:
            continue
        if now >= created_dt:
            with db_connect() as conn2:
                c2 = conn2.cursor()
                c2.execute("UPDATE lotteries SET msg_count = msg_count + 1 WHERE id=? AND status=0", (lid,))
                conn2.commit()

    # ===== 骰子下注 =====
    dice_match = re.match(r'^押\s+(\S+)\s+(\d+(?:\.\d+)?)$', text) or re.match(r'^押\s+(\d+(?:\.\d+)?)\s+(\S+)$', text)
    if dice_match:
        if dice_match.group(1).replace('.', '').isdigit():
            amount = float(dice_match.group(1))
            bet_type = dice_match.group(2)
        else:
            bet_type = dice_match.group(1)
            amount = float(dice_match.group(2))
        valid_bets = ['大', '小', '单', '双', '大单', '大双', '小单', '小双']
        if bet_type not in valid_bets:
            await update.message.reply_text("玩法错误，请选择：大、小、单、双、大单、大双、小单、小双")
            return
        if amount <= 0:
            await update.message.reply_text("下注金额必须为正数。")
            return

        state = get_dice_state()
        if state['status'] != 'active':
            rid = create_new_round()
            chat_id = update.effective_chat.id
            asyncio.create_task(round_timer(ctx, rid, chat_id))
            state = get_dice_state()
            if state['status'] != 'active':
                await update.message.reply_text("游戏初始化失败，请稍后再试。")
                return

        round_id = state['round_id']
        uid = update.message.from_user.id
        name = update.message.from_user.first_name

        get_user(uid, name)
        bal = get_coins(uid)
        if bal < amount:
            await update.message.reply_text(f"余额不足！你需要 {amount} 学分，当前余额 {bal:.2f}。")
            return
        if not sub_coins(uid, amount, f"骰子下注 {bet_type}"):
            await update.message.reply_text("下注失败，请稍后再试。")
            return

        with db_connect() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dice_bets (round_id, user_id, amount, bet_type, win) VALUES (?,?,?,?,?)",
                      (round_id, uid, amount, bet_type, None))
            conn.commit()

        end_time = datetime.fromisoformat(state['end_time'])
        remaining_seconds = max(0, int((end_time - now_cn()).total_seconds()))
        date_str = now_cn().strftime('%m月%d日')
        rid = round_id
        bet_example = "押 大 10  押 小 10  押大双 10"
        status = "🟢投注中"
        msg = f"🎲同学会骰王 {date_str} 第{rid}期\n"
        msg += f"💡状态：    {status}\n"
        msg += f"⏰️距离开奖:{remaining_seconds}秒\n"
        msg += f"💰投注格式：{bet_example}"
        await update.message.reply_text(msg)
        return

    # 普通发言掉落
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

# ========== 骰子游戏核心 ==========
DICE_INTERVAL = 180
RAKE = 0.10

def get_dice_state():
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM dice_state WHERE key='current_round'")
        row = c.fetchone()
        rid = int(row[0]) if row else 0
        c.execute("SELECT value FROM dice_state WHERE key='end_time'")
        row = c.fetchone()
        end_time_str = row[0] if row else ''
        status = 'active' if end_time_str and now_cn() < datetime.fromisoformat(end_time_str) else 'inactive'
        return {'round_id': rid, 'end_time': end_time_str, 'status': status}

def create_new_round():
    with db_connect() as conn:
        c = conn.cursor()
        now = now_cn()
        end_time = now + timedelta(seconds=DICE_INTERVAL)
        c.execute("INSERT INTO dice_rounds (start_time, end_time, numbers, total, result, total_bets) VALUES (?, ?, ?, ?, ?, ?)",
                  (now, end_time, None, None, None, 0))
        rid = c.lastrowid
        c.execute("REPLACE INTO dice_state (key, value) VALUES ('current_round', ?)", (str(rid),))
        c.execute("REPLACE INTO dice_state (key, value) VALUES ('end_time', ?)", (end_time.isoformat(),))
        conn.commit()
        print(f"创建新轮: rid={rid}, end_time={end_time.isoformat()}")
        return rid

def close_round(rid, numbers, total, result, total_bets):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE dice_rounds SET numbers=?, total=?, result=?, total_bets=? WHERE id=?",
                  (numbers, total, result, total_bets, rid))
        conn.commit()

def get_bets_for_round(rid):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, amount, bet_type FROM dice_bets WHERE round_id=? AND win IS NULL", (rid,))
        return c.fetchall()

def update_bet_win(rid, uid, win_amount):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE dice_bets SET win=? WHERE round_id=? AND user_id=?", (win_amount, rid, uid))
        conn.commit()

def get_dice_win_rate(uid):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM dice_bets WHERE user_id=? AND win IS NOT NULL", (uid,))
        total = c.fetchone()[0]
        if total == 0:
            return 0, 0
        c.execute("SELECT COUNT(*) FROM dice_bets WHERE user_id=? AND win > 0", (uid,))
        wins = c.fetchone()[0]
        return wins, total

def generate_dice_numbers():
    return [random.randint(0, 9) for _ in range(3)]

async def round_timer(context, rid, chat_id):
    print(f"定时器启动，等待 {DICE_INTERVAL} 秒后结算第{rid}期")
    await asyncio.sleep(DICE_INTERVAL)
    print(f"定时器触发，结算第{rid}期")
    await settle_round(context, rid, chat_id)

async def settle_round(context, rid, chat_id):
    print(f">>> 结算第{rid}期")
    bets = get_bets_for_round(rid)
    if not bets:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("UPDATE dice_rounds SET end_time=?, numbers='', total=0, result='无人下注', total_bets=0 WHERE id=?", (now_cn(), rid))
            conn.commit()
        await context.bot.send_message(chat_id=chat_id, text=f"🎲 同学会骰王 第{rid}期 无人下注，已结束。")
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM dice_bets WHERE round_id = ?", (rid,))
            c.execute("DELETE FROM dice_rounds WHERE id = ?", (rid,))
            conn.commit()
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("REPLACE INTO dice_state (key, value) VALUES ('current_round', '0')")
            c.execute("REPLACE INTO dice_state (key, value) VALUES ('end_time', '')")
            conn.commit()
        return
    numbers = generate_dice_numbers()
    total = sum(numbers)
    is_big = total >= 14
    is_odd = total % 2 == 1
    result_type = []
    if is_big:
        result_type.append('大')
    else:
        result_type.append('小')
    if is_odd:
        result_type.append('单')
    else:
        result_type.append('双')
    result_combined = ''.join(result_type)
    winners = []
    for uid, amount, bet_type in bets:
        win = 0
        if bet_type in ['大', '小', '单', '双']:
            if bet_type in result_type:
                win = amount
        elif bet_type in ['大单', '大双', '小单', '小双']:
            if bet_type == result_combined:
                win = amount * 3
        if win > 0:
            win_after_rake = win * (1 - RAKE)
            add_coins(uid, amount + win_after_rake, f"骰子中奖 {bet_type}")
            winners.append((uid, amount, bet_type, win_after_rake))
            update_bet_win(rid, uid, win_after_rake)
        else:
            update_bet_win(rid, uid, 0.0)
    close_round(rid, '-'.join(map(str, numbers)), total, result_combined, len(bets))
    date_str = now_cn().strftime('%m月%d日')
    result_msg = f"<b>🎲 同学会骰王  {date_str} 第{rid}期 开奖结果</b>\n"
    result_msg += f"🎯号码：{' + '.join(map(str, numbers))} = {total}\n"
    result_msg += f"📋结果：<b>{result_combined}</b>\n\n"
    if winners:
        result_msg += "🏆 中奖名单：\n"
        for uid, amount, bet_type, win in winners:
            with db_connect() as conn:
                c = conn.cursor()
                c.execute("SELECT nickname FROM users WHERE user_id=?", (uid,))
                row = c.fetchone()
                name = row[0] if row else str(uid)
            link = f'<a href="tg://user?id={uid}">{name}</a>'
            result_msg += f"  {link} 押{bet_type}{amount}学分 → +{win:.2f}学分\n"
    else:
        result_msg += "😭本期无人中奖\n"
    result_msg += "\n⏰下一期即将开始，请下注"
    await context.bot.send_message(chat_id=chat_id, text=result_msg, parse_mode=ParseMode.HTML)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM dice_bets WHERE round_id = ?", (rid,))
        c.execute("DELETE FROM dice_rounds WHERE id = ?", (rid,))
        conn.commit()
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("REPLACE INTO dice_state (key, value) VALUES ('current_round', '0')")
        c.execute("REPLACE INTO dice_state (key, value) VALUES ('end_time', '')")
        conn.commit()

async def dice_stats(update, ctx):
    if update.message.from_user.is_bot:
        return
    uid = update.effective_user.id
    wins, total = get_dice_win_rate(uid)
    if total == 0:
        await update.message.reply_text("您还没有参与过骰子游戏记录。")
    else:
        rate = wins / total * 100
        await update.message.reply_text(f"🎲 您的骰子战绩：\n胜场：{wins}\n总局数：{total}\n胜率：{rate:.1f}%")

# ========== 启动 ==========
def main():
    init_db()
    # 修复事件循环弃用警告
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(TOKEN).build()
    bot = app.bot
    loop.create_task(auto_draw_loop(bot))

    # 群聊中的 /学分 由 MessageHandler 处理（保留原有）
    app.add_handler(MessageHandler(filters.Regex(r'^/学分'), admin_credit_handler))
    # 私聊中的 /credit 命令（英文命令，用于私聊加学分）
    app.add_handler(CommandHandler("credit", admin_credit_private, filters=filters.ChatType.PRIVATE))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("coins", cmd_coins))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(MessageHandler(filters.Regex(r'^骰子战绩$'), dice_stats))
    app.add_handler(CommandHandler("dice_stats", dice_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(CommandHandler("test", test_callback))
    app.add_handler(CallbackQueryHandler(test_cb, pattern="^test$"))
    app.add_handler(CommandHandler("additem", admin_add_item))
    app.add_handler(CommandHandler("listitems", admin_list_items))
    app.add_handler(CommandHandler("delitem", admin_del_item))
    app.add_handler(CommandHandler("cj", cmd_create_lottery))
    app.add_handler(CommandHandler("cjlist", cmd_list_lotteries))
    app.add_handler(CommandHandler("qx", cmd_cancel_lottery))
    app.add_handler(CommandHandler("gg", cmd_change_time))
    app.add_handler(CommandHandler("ql", cmd_clean_lottery))
    app.add_handler(CommandHandler("sb", cmd_remove_user_lottery))
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
