import os
import random
import sqlite3
import re
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler

# ========== 配置 ==========
TOKEN = "8686804795:AAFe6KzzbHABTz8o7UwzyquU4DWMWqBamHg"  # 请替换为实际Token
ALLOWED_GROUPS = [-1003002241602, -1003745425265, -1003720878201]   # 仅允许这两个群组
ADMIN_IDS = [8354445328, 877039616]

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
        # ------- 骰子游戏表 -------
        c.execute("CREATE TABLE IF NOT EXISTS dice_rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, start_time TIMESTAMP, end_time TIMESTAMP, numbers TEXT, total INT, result TEXT, total_bets INT)")
        c.execute("CREATE TABLE IF NOT EXISTS dice_bets (round_id INT, user_id INT, amount REAL, bet_type TEXT, win REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS dice_state (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR IGNORE INTO dice_state (key, value) VALUES ('current_round', '0')")
        c.execute("INSERT OR IGNORE INTO dice_state (key, value) VALUES ('end_time', '')")
        # ------- 全局限量表 -------
        c.execute("CREATE TABLE IF NOT EXISTS global_limits (item_id INT PRIMARY KEY)")
        c.execute("PRAGMA table_info(global_limits)")
        cols = [col[1] for col in c.fetchall()]
        if "remaining" not in cols:
            c.execute("ALTER TABLE global_limits ADD COLUMN remaining INT DEFAULT 0")
        c.execute("INSERT OR IGNORE INTO global_limits (item_id, remaining) VALUES (3, 1)")
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
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT used FROM daily_first_bonus WHERE user_id=? AND date=?", (uid, today))
        row = c.fetchone()
        if row and row[0] == 1:
            return False
        c.execute("INSERT OR REPLACE INTO daily_first_bonus (user_id, date, used) VALUES (?,?,1)", (uid, today))
        conn.commit()
        return True

def get_remaining(item_id):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT remaining FROM global_limits WHERE item_id=?", (item_id,))
        row = c.fetchone()
        return row[0] if row else 0

def decrease_remaining(item_id):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE global_limits SET remaining = remaining - 1 WHERE item_id=? AND remaining > 0", (item_id,))
        conn.commit()
        return c.rowcount > 0

# ========== 键盘 ==========
def wallet_kb():
    return Markup([
        [Btn("🛒 兑换商品", callback_data="shop"),
         Btn("📚 学分记录", callback_data="history")]
    ])

def shop_kb():
    keyboard = []
    for i, n, p in SHOP:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT remaining FROM global_limits WHERE item_id=?", (i,))
            row = c.fetchone()
        if row:
            rem = row[0]
            if rem > 0:
                text = f"{n} 剩余{rem} - {p}💎"
            else:
                text = f"{n} 已售罄 - {p}💎"
        else:
            text = f"{n} - {p}💎"
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

        if iid == 3:
            remaining = get_remaining(3)
            if remaining <= 0:
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
            if iid == 3:
                decrease_remaining(3)
            new_balance = get_coins(uid)
            # 编辑原消息
            await query.edit_message_text(
                f"✅ {n} 兑换成功！消耗 {p} 学分",
                reply_markup=Markup([[Btn("🔙 返回钱包", callback_data="back")]])
            )
            # 群组中公开通知（不带@管理员）
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
            # 私聊通知每位管理员
            for aid in ADMIN_IDS:
                try:
                    admin_msg = f"用户 {name}（ID: {uid}）兑换了 {n}，消耗 {p} 学分。"
                    await ctx.bot.send_message(
                        chat_id=aid,
                        text=admin_msg
                    )
                except Exception as e:
                    print(f"私聊管理员 {aid} 失败: {e}")
            # 私聊购买记录
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
        if gid == 3:
            rem = get_remaining(3)
            text += f"ID:{gid} {name} - {price}💎 (剩余{rem}/1)\n"
        else:
            with db_connect() as conn:
                c = conn.cursor()
                c.execute("SELECT remaining FROM global_limits WHERE item_id=?", (gid,))
                row = c.fetchone()
            if row:
                rem = row[0]
                text += f"ID:{gid} {name} - {price}💎 (剩余{rem})\n"
            else:
                text += f"ID:{gid} {name} - {price}💎\n"
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
                "/学分 +数字 或 /学分 -数字 - 修改用户学分（需回复用户消息）\n"
                "/coins - 查询自己学分\n"
                "/shop - 打开商城\n"
                "/start - 显示本帮助"
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

    # ===== 发送“学分”显示余额 =====
    if text == "学分":
        uid = update.message.from_user.id
        name = update.message.from_user.first_name
        bal = get_coins(uid)
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        await update.message.reply_text(f"💰 {link}，你的当前余额是 {bal:.2f} 学分。", parse_mode=ParseMode.HTML)
        return

    # ===== 学分排行榜 =====
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

    if text.startswith('/'):
        return

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
        round_id = state['round_id']
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dice_bets (round_id, user_id, amount, bet_type, win) VALUES (?,?,?,?,?)",
                      (round_id, uid, amount, bet_type, None))
            conn.commit()

        # 美化投注成功消息
        end_time = datetime.fromisoformat(state['end_time'])
        remaining_seconds = max(0, int((end_time - datetime.now()).total_seconds()))
        date_str = datetime.now().strftime('%m月%d日')
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
DICE_INTERVAL = 120
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
        status = 'active' if end_time_str and datetime.now() < datetime.fromisoformat(end_time_str) else 'inactive'
        return {'round_id': rid, 'end_time': end_time_str, 'status': status}

def create_new_round():
    with db_connect() as conn:
        c = conn.cursor()
        now = datetime.now()
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
            c.execute("UPDATE dice_rounds SET end_time=?, numbers='', total=0, result='无人下注', total_bets=0 WHERE id=?", (datetime.now(), rid))
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
            # 返还本金 + 净收益
            add_coins(uid, amount + win_after_rake, f"骰子中奖 {bet_type}")
            winners.append((uid, amount, bet_type, win_after_rake))
            update_bet_win(rid, uid, win_after_rake)
        else:
            update_bet_win(rid, uid, 0.0)
    close_round(rid, '-'.join(map(str, numbers)), total, result_combined, len(bets))

    date_str = datetime.now().strftime('%m月%d日')
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
            result_msg += f"  {name} 押{bet_type}{amount}学分 → +{win:.2f}学分\n"
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
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("coins", cmd_coins))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(MessageHandler(filters.Regex(r'^/学分'), admin_credit_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^骰子战绩$'), dice_stats))
    app.add_handler(CommandHandler("dice_stats", dice_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(CommandHandler("test", test_callback))
    app.add_handler(CallbackQueryHandler(test_cb, pattern="^test$"))
    app.add_handler(CommandHandler("additem", admin_add_item))
    app.add_handler(CommandHandler("listitems", admin_list_items))
    app.add_handler(CommandHandler("delitem", admin_del_item))
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
