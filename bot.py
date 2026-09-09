#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG 打卡上班机器人 - 完整增强版
功能：
- 开课/下课、出勤按钮、爱心反应、7天清理、管理员面板
- 频道定时发送（湖州/嘉兴独立配置）
- 个人有效期（管理员设置，超期私聊阻止开课）
"""

import json
import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ==================== 配置 ====================
BOT_TOKEN = "8179579064:AAF57RUAH5TVtrW4qdA4_wIAtWkRAAkqkvo"
GROUP_A_CHAT_ID = -1003689306909
GROUP_B_CHAT_ID = -1003002241602
GROUP_C_CHAT_ID = -1003745425265
ADMIN_IDS = {8354445328, 877039616, 42438298, 34415852}
# ============================================

DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
ACTIVE_FILE = os.path.join(DATA_DIR, "active.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# 对话状态
ADMIN_ADD_ID, ADMIN_ADD_NAME, ADMIN_ADD_REGION = range(1, 4)
ADMIN_SET_CHANNEL, ADMIN_SET_INTERVAL, ADMIN_SET_EXPIRE = range(10, 13)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info(f"数据目录: {DATA_DIR}")

# ---------- 数据持久化 ----------
def load_users() -> Dict[int, Dict]:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    return {}

def save_users(users: Dict[int, Dict]):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def load_active() -> Dict[int, Dict]:
    if os.path.exists(ACTIVE_FILE):
        with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    return {}

def save_active(active: Dict[int, Dict]):
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(active, f, ensure_ascii=False, indent=2)

def load_config() -> Dict:
    default = {
        "channel_b": {"chat_id": None, "interval_hours": 24, "enabled": False},
        "channel_c": {"chat_id": None, "interval_hours": 24, "enabled": False},
        "last_sent_b": None,
        "last_sent_c": None,
        # 个人有效期存储格式: {"user_id": "2026-12-31T23:59:59+00:00"}
        "user_expires": {}
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                for key in default:
                    if key not in data:
                        data[key] = default[key]
                return data
            except:
                return default
    return default

def save_config(config: Dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ---------- 原有函数 ----------
def reset_active_status():
    active = load_active()
    if active:
        save_active({})
        logger.info("定时任务：已清空所有开课状态")

def cleanup_inactive_users():
    users = load_users()
    if not users:
        return
    now = datetime.now(timezone.utc)
    deleted = []
    for uid, info in users.items():
        last_active_str = info.get("last_active")
        if not last_active_str:
            continue
        try:
            last_active = datetime.fromisoformat(last_active_str)
        except:
            continue
        if (now - last_active).days >= 7:
            deleted.append(uid)
            active = load_active()
            if uid in active:
                del active[uid]
                save_active(active)
    if deleted:
        for uid in deleted:
            del users[uid]
        save_users(users)
        logger.info(f"清理了 {len(deleted)} 名连续7天未开课的老师，ID: {deleted}")

def get_user_info(user_id: int) -> Optional[Dict]:
    return load_users().get(user_id)

def set_user_info(user_id: int, name: str, region: str):
    users = load_users()
    users[user_id] = {"name": name, "region": region}
    save_users(users)

def delete_user_info(user_id: int):
    users = load_users()
    if user_id in users:
        del users[user_id]
        save_users(users)
        active = load_active()
        if user_id in active:
            del active[user_id]
            save_active(active)
        # 同时清除有效期
        config = load_config()
        if str(user_id) in config.get("user_expires", {}):
            del config["user_expires"][str(user_id)]
            save_config(config)

def get_attendance_buttons(region: str) -> Optional[InlineKeyboardMarkup]:
    users = load_users()
    active = load_active()
    region_users = {uid: info for uid, info in users.items() if info.get("region") == region}
    if not region_users:
        return None

    active_list = []
    rest_list = []
    for uid, info in region_users.items():
        if uid in active:
            active_list.append((uid, info, active[uid].get("start_time", "")))
        else:
            rest_list.append((uid, info))
    active_list.sort(key=lambda x: x[2])
    rest_list.sort(key=lambda x: x[1]["name"])
    sorted_items = [(uid, info) for uid, info, _ in active_list] + [(uid, info) for uid, info in rest_list]

    buttons = []
    for uid, info in sorted_items:
        status = "🟢" if uid in active else "🔴"
        button_text = f"{status} {info['name']}"
        buttons.append(InlineKeyboardButton(button_text, url=f"tg://user?id={uid}"))
    keyboard = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(keyboard)

async def is_member_of_group_a(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=GROUP_A_CHAT_ID, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        logger.error(f"检查群A成员失败: {e}")
        return False

# ---------- 私聊 /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("请私聊我使用 /start。")
        return

    user_id = update.effective_user.id
    if not await is_member_of_group_a(user_id, context):
        await update.message.reply_text("❌ 您尚未加入开课群，无法使用机器人。请先加入群A。")
        return

    user_info = get_user_info(user_id)
    if user_info:
        text = f"✅ 已登记：{user_info['name']}（{user_info['region']}同学会）"
    else:
        text = "🤖 请完成登记"

    keyboard = [
        [InlineKeyboardButton("📝 登记/修改昵称", callback_data="set_name")],
        [InlineKeyboardButton("🌍 设置地区（湖州/嘉兴）", callback_data="set_region")],
        [InlineKeyboardButton("👤 查看我的信息", callback_data="my_info")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 管理员面板", callback_data="admin_panel")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ---------- 回调处理 ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "admin_panel":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("无权访问")
            return
        keyboard = [
            [InlineKeyboardButton("📋 查看所有登记老师", callback_data="admin_list_users")],
            [InlineKeyboardButton("🟢 查看当前开课老师", callback_data="admin_list_active")],
            [InlineKeyboardButton("❌ 强制清空某个老师开课", callback_data="admin_clear_active")],
            [InlineKeyboardButton("🗑 删除登记老师", callback_data="admin_delete_user")],
            [InlineKeyboardButton("➕ 手动添加登记老师", callback_data="admin_add_user")],
            [InlineKeyboardButton("📡 设置湖州频道", callback_data="admin_set_channel_b")],
            [InlineKeyboardButton("📡 设置嘉兴频道", callback_data="admin_set_channel_c")],
            [InlineKeyboardButton("⏰ 设置个人有效期", callback_data="admin_set_expire")],
            [InlineKeyboardButton("🔙 返回", callback_data="back_to_menu")],
        ]
        await query.edit_message_text("管理员面板：", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # 设置频道
    if data in ["admin_set_channel_b", "admin_set_channel_c"]:
        if user_id not in ADMIN_IDS:
            return
        channel = "b" if data == "admin_set_channel_b" else "c"
        context.user_data["set_channel"] = channel
        await query.edit_message_text(f"请输入 {channel.upper()} 频道的 Chat ID（负数，例如 -1001234567890）：")
        context.user_data["admin_set_step"] = ADMIN_SET_CHANNEL
        return

    # 设置个人有效期
    if data == "admin_set_expire":
        if user_id not in ADMIN_IDS:
            return
        await query.edit_message_text("请输入要设置有效期的老师ID（数字）：")
        context.user_data["admin_set_step"] = ADMIN_SET_EXPIRE
        return

    # 原有回调
    if data == "admin_add_user":
        if user_id not in ADMIN_IDS:
            return
        await query.edit_message_text("请输入要添加的老师数字ID（例如 123456789）：")
        context.user_data["admin_add_step"] = ADMIN_ADD_ID
        return

    if data == "admin_list_users":
        if user_id not in ADMIN_IDS:
            return
        users = load_users()
        if not users:
            await query.edit_message_text("暂无登记老师")
            return
        lines = ["已登记老师列表："]
        for uid, info in users.items():
            last_active = info.get("last_active", "从未开课")
            lines.append(f"{uid} | {info['name']} | {info['region']} | 最后活跃: {last_active}")
        text = "\n".join(lines)
        if len(text) > 4000:
            await query.edit_message_text("列表过长，请查看日志")
            logger.info(text)
        else:
            await query.edit_message_text(text)
        return

    if data == "admin_list_active":
        if user_id not in ADMIN_IDS:
            return
        active = load_active()
        if not active:
            await query.edit_message_text("当前无开课老师")
            return
        lines = ["开课老师列表："]
        for uid, info in active.items():
            lines.append(f"{uid} | {info['name']} | {info['region']} | 开始时间 {info['start_time']}")
        text = "\n".join(lines)
        await query.edit_message_text(text)
        return

    if data == "admin_clear_active":
        if user_id not in ADMIN_IDS:
            return
        await query.edit_message_text("请输入要强制下课的老师的数字ID（仅数字）：")
        context.user_data["admin_clear_target"] = True
        return

    if data == "admin_delete_user":
        if user_id not in ADMIN_IDS:
            return
        await query.edit_message_text("请输入要删除的老师数字ID：")
        context.user_data["admin_delete_target"] = True
        return

    if data == "set_name":
        if not await is_member_of_group_a(user_id, context):
            await query.edit_message_text("❌ 您未加入开课群，无法设置昵称。")
            return
        await query.edit_message_text("请输入您的昵称：")
        context.user_data["awaiting_name"] = True
        return

    if data == "set_region":
        if not await is_member_of_group_a(user_id, context):
            await query.edit_message_text("❌ 您未加入开课群，无法设置地区。")
            return
        keyboard = [
            [InlineKeyboardButton("湖州同学会", callback_data="region_湖州")],
            [InlineKeyboardButton("嘉兴同学会", callback_data="region_嘉兴")],
            [InlineKeyboardButton("返回", callback_data="back_to_menu")],
        ]
        await query.edit_message_text("选择地区：", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("region_"):
        region = data.split("_")[1]
        user_info = get_user_info(user_id)
        if not user_info or "name" not in user_info:
            await query.edit_message_text("请先设置昵称")
            return
        set_user_info(user_id, user_info["name"], region)
        await query.edit_message_text(f"✅ 地区已设置为 {region}同学会")
        return

    if data == "my_info":
        user_info = get_user_info(user_id)
        if not user_info:
            await query.edit_message_text("未登记")
            return
        active = load_active()
        status = "🟢 开课中" if user_id in active else "🔴 未开课"
        # 显示有效期（如果有）
        config = load_config()
        expire_str = config.get("user_expires", {}).get(str(user_id))
        if expire_str:
            try:
                expire_dt = datetime.fromisoformat(expire_str)
                if expire_dt > datetime.now(timezone.utc):
                    status += f"\n⏰ 有效期至: {expire_dt.strftime('%Y-%m-%d %H:%M')}"
                else:
                    status += "\n⛔ 已过期（请联系管理员续期）"
            except:
                pass
        text = f"昵称：{user_info['name']}\n地区：{user_info['region']}同学会\n状态：{status}"
        await query.edit_message_text(text)
        return

    if data == "back_to_menu":
        user_info = get_user_info(user_id)
        if user_info:
            text = f"✅ 已登记：{user_info['name']}（{user_info['region']}同学会）"
        else:
            text = "🤖 请完成登记"
        keyboard = [
            [InlineKeyboardButton("📝 登记/修改昵称", callback_data="set_name")],
            [InlineKeyboardButton("🌍 设置地区（湖州/嘉兴）", callback_data="set_region")],
            [InlineKeyboardButton("👤 查看我的信息", callback_data="my_info")],
        ]
        if user_id in ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("🔧 管理员面板", callback_data="admin_panel")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

# ---------- 私聊文本处理 ----------
async def private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ----- 设置频道 Chat ID -----
    if context.user_data.get("admin_set_step") == ADMIN_SET_CHANNEL:
        channel = context.user_data.get("set_channel")
        if not channel:
            await update.message.reply_text("会话已过期，请重新开始")
            return
        try:
            chat_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字 Chat ID：")
            return
        # 保存 Chat ID
        config = load_config()
        if channel == "b":
            config["channel_b"]["chat_id"] = chat_id
            config["channel_b"]["enabled"] = True
        else:
            config["channel_c"]["chat_id"] = chat_id
            config["channel_c"]["enabled"] = True
        save_config(config)
        await update.message.reply_text(f"✅ 已设置 {channel.upper()} 频道 Chat ID: {chat_id}\n接下来请设置发送间隔（小时数）：")
        # 切换到间隔设置状态
        context.user_data["admin_set_step"] = ADMIN_SET_INTERVAL
        return

    # ----- 设置发送间隔 -----
    if context.user_data.get("admin_set_step") == ADMIN_SET_INTERVAL:
        channel = context.user_data.get("set_channel")
        if not channel:
            await update.message.reply_text("会话已过期，请重新开始")
            return
        try:
            interval = float(text)
            if interval <= 0:
                raise ValueError
        except:
            await update.message.reply_text("❌ 请输入正数（小时），例如 24")
            return
        config = load_config()
        if channel == "b":
            config["channel_b"]["interval_hours"] = interval
            config["last_sent_b"] = None
        else:
            config["channel_c"]["interval_hours"] = interval
            config["last_sent_c"] = None
        save_config(config)
        await update.message.reply_text(f"✅ 设置完成！每隔 {interval} 小时自动发送出勤到 {channel.upper()} 频道。")
        context.user_data.pop("admin_set_step", None)
        context.user_data.pop("set_channel", None)
        return

    # ----- 设置个人有效期（输入用户ID）-----
    if context.user_data.get("admin_set_step") == ADMIN_SET_EXPIRE:
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字ID：")
            return
        if not get_user_info(target_id):
            await update.message.reply_text(f"⚠️ 老师 ID {target_id} 未登记，请先添加老师。")
            context.user_data.pop("admin_set_step", None)
            return
        context.user_data["expire_target"] = target_id
        await update.message.reply_text("请输入有效期天数（例如 30，输入 0 表示无限期）：")
        context.user_data["admin_set_step"] = ADMIN_SET_INTERVAL  # 重用间隔状态，但含义不同，实际为有效期天数
        # 改变标志以免混淆
        context.user_data["expire_days_mode"] = True
        return

    # ----- 接收有效期天数 -----
    if context.user_data.get("admin_set_step") == ADMIN_SET_INTERVAL and context.user_data.get("expire_days_mode"):
        target_id = context.user_data.get("expire_target")
        if not target_id:
            await update.message.reply_text("会话已过期，请重新开始")
            return
        try:
            days = int(text)
            if days < 0:
                raise ValueError
        except:
            await update.message.reply_text("❌ 请输入非负整数（0 表示无限期）：")
            return
        config = load_config()
        if days == 0:
            # 清除有效期
            if str(target_id) in config.get("user_expires", {}):
                del config["user_expires"][str(target_id)]
                save_config(config)
            await update.message.reply_text(f"✅ 已取消老师 {target_id} 的有效期限制（无限期）。")
        else:
            expire_time = datetime.now(timezone.utc) + timedelta(days=days)
            expire_str = expire_time.isoformat()
            if "user_expires" not in config:
                config["user_expires"] = {}
            config["user_expires"][str(target_id)] = expire_str
            save_config(config)
            await update.message.reply_text(f"✅ 老师 {target_id} 的有效期已设置为 {days} 天，到期时间为 {expire_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        context.user_data.pop("admin_set_step", None)
        context.user_data.pop("expire_target", None)
        context.user_data.pop("expire_days_mode", None)
        return

    # ----- 原有管理员添加老师 ----------
    step = context.user_data.get("admin_add_step")
    if step == ADMIN_ADD_ID:
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字ID：")
            return
        if get_user_info(target_id):
            await update.message.reply_text(f"⚠️ 老师 ID {target_id} 已登记，请勿重复添加。")
            context.user_data.pop("admin_add_step", None)
            return
        context.user_data["admin_add_target_id"] = target_id
        context.user_data["admin_add_step"] = ADMIN_ADD_NAME
        await update.message.reply_text("请输入老师的昵称（例如：王老师）：")
        return

    if step == ADMIN_ADD_NAME:
        name = text.strip()
        if not name:
            await update.message.reply_text("昵称不能为空，请重新输入：")
            return
        context.user_data["admin_add_name"] = name
        context.user_data["admin_add_step"] = ADMIN_ADD_REGION
        keyboard = [
            [InlineKeyboardButton("湖州同学会", callback_data="admin_add_region_湖州")],
            [InlineKeyboardButton("嘉兴同学会", callback_data="admin_add_region_嘉兴")],
        ]
        await update.message.reply_text("请选择该老师的地区：", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ----- 原有管理员强制下课 ----------
    if context.user_data.get("admin_clear_target"):
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("请输入数字ID")
            context.user_data.pop("admin_clear_target", None)
            return
        active = load_active()
        if target_id in active:
            info = active.pop(target_id)
            save_active(active)
            await update.message.reply_text(f"✅ 已强制下课 {info['name']} (ID:{target_id})")
        else:
            await update.message.reply_text(f"ID {target_id} 当前未开课")
        context.user_data.pop("admin_clear_target", None)
        return

    # ----- 原有管理员删除老师 ----------
    if context.user_data.get("admin_delete_target"):
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("请输入数字ID")
            context.user_data.pop("admin_delete_target", None)
            return
        user_info = get_user_info(target_id)
        if user_info:
            delete_user_info(target_id)
            await update.message.reply_text(f"✅ 已删除老师 {user_info['name']} (ID:{target_id})")
        else:
            await update.message.reply_text(f"ID {target_id} 未登记")
        context.user_data.pop("admin_delete_target", None)
        return

    # ----- 原有普通用户设置昵称 ----------
    if context.user_data.get("awaiting_name"):
        if not await is_member_of_group_a(user_id, context):
            await update.message.reply_text("❌ 您未加入开课群，无法设置昵称。")
            context.user_data.pop("awaiting_name", None)
            return
        new_name = text
        if not new_name:
            await update.message.reply_text("昵称不能为空")
            return
        user_info = get_user_info(user_id)
        region = user_info.get("region") if user_info else ""
        set_user_info(user_id, new_name, region)
        await update.message.reply_text(f"✅ 昵称已设置为 {new_name}\n请继续设置地区（/start）")
        context.user_data.pop("awaiting_name", None)

# ---------- 管理员添加老师地区选择回调 ----------
async def admin_add_region_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("无权操作")
        return
    if context.user_data.get("admin_add_step") != ADMIN_ADD_REGION:
        await query.edit_message_text("流程已过期，请重新开始")
        return
    region = query.data.split("_")[-1]
    target_id = context.user_data.get("admin_add_target_id")
    name = context.user_data.get("admin_add_name")
    if not target_id or not name:
        await query.edit_message_text("数据丢失，请重新添加")
        context.user_data.pop("admin_add_step", None)
        return
    users = load_users()
    users[target_id] = {"name": name, "region": region}
    save_users(users)
    await query.edit_message_text(f"✅ 已成功添加老师：{name}（{region}同学会）ID: {target_id}")
    context.user_data.pop("admin_add_step", None)
    context.user_data.pop("admin_add_target_id", None)
    context.user_data.pop("admin_add_name", None)

# ---------- 群组消息处理 ----------
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    raw_text = update.message.text.strip()
    user = update.effective_user
    if not user:
        return

    text = raw_text.split('@')[0].strip()

    if chat_id == GROUP_A_CHAT_ID:
        user_id = user.id

        # 获取用户登记信息
        user_info = get_user_info(user_id)
        if not user_info or not user_info.get("name") or not user_info.get("region"):
            if user_id in ADMIN_IDS:
                return
            bot_username = context.bot.username
            url = f"https://t.me/{bot_username}" if bot_username else "https://t.me/"
            keyboard = [[InlineKeyboardButton("📩 点击这里与机器人私聊进行登记", url=url)]]
            await update.message.reply_text(
                "❌ 您尚未登记，请先私聊机器人完成登记。",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # 检查个人有效期（仅针对开课命令）
        if text == "开课":
            config = load_config()
            expire_str = config.get("user_expires", {}).get(str(user_id))
            if expire_str:
                try:
                    expire_dt = datetime.fromisoformat(expire_str)
                    if datetime.now(timezone.utc) >= expire_dt:
                        # 已过期，私聊通知，群内无任何提示
                        await update.message.reply_text("⛔ 您的登记已过期，请联系管理员续期。")
                        logger.info(f"用户 {user_info['name']} (ID:{user_id}) 尝试开课但已过期，已私聊通知")
                        return
                except:
                    pass

        # 已登记，处理开课/下课
        active = load_active()
        if text == "开课":
            if user_id in active:
                await update.message.reply_text("已在开课中")
                return
            user_info["last_active"] = datetime.now(timezone.utc).isoformat()
            users = load_users()
            users[user_id] = user_info
            save_users(users)
            active[user_id] = {
                "name": user_info["name"],
                "region": user_info["region"],
                "start_time": datetime.now().isoformat()
            }
            save_active(active)
            await update.message.reply_text(f"✅ {user_info['name']} 开课成功 🟢")
            return
        elif text == "下课":
            if user_id not in active:
                await update.message.reply_text("未开课")
                return
            info = active.pop(user_id)
            save_active(active)
            await update.message.reply_text(f"✅ {info['name']} 下课 🔴")
            return
        return

    if chat_id == GROUP_B_CHAT_ID and text == "出勤":
        keyboard = get_attendance_buttons("湖州")
        if keyboard:
            await update.message.reply_text("湖州同学会出勤：", reply_markup=keyboard)
        else:
            await update.message.reply_text("暂无老师")
        return

    if chat_id == GROUP_C_CHAT_ID and text == "出勤":
        keyboard = get_attendance_buttons("嘉兴")
        if keyboard:
            await update.message.reply_text("嘉兴同学会出勤：", reply_markup=keyboard)
        else:
            await update.message.reply_text("暂无老师")
        return

# ---------- 自动爱心反应 ----------
async def auto_heart_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if not user or user.id == context.bot.id:
        return
    if not get_user_info(user.id):
        return
    try:
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction=['❤']
        )
        logger.info(f"❤️ 已给 {user.id} 的消息加心")
    except Exception as e:
        logger.error(f"加心失败: {e}")

# ---------- 定时发送到频道 ----------
async def send_attendance_to_channel(bot, region: str, channel_key: str):
    config = load_config()
    ch_cfg = config[f"channel_{channel_key}"]
    if not ch_cfg.get("enabled") or not ch_cfg.get("chat_id"):
        logger.error(f"错误：{region} 频道未设置或未启用")
        return
    chat_id = ch_cfg["chat_id"]
    keyboard = get_attendance_buttons(region)
    if not keyboard:
        text = f"📭 {region}同学会当前暂无登记老师。"
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error(f"发送 {region} 出勤到频道失败: {e}")
    else:
        try:
            await bot.send_message(chat_id=chat_id, text=f"📢 {region}同学会定时出勤报告：", reply_markup=keyboard)
            logger.info(f"已发送 {region} 出勤到频道 {chat_id}")
            # 更新最后发送时间
            config[f"last_sent_{channel_key}"] = datetime.now(timezone.utc).isoformat()
            save_config(config)
        except Exception as e:
            logger.error(f"发送 {region} 出勤到频道失败: {e}")

# ---------- 后台任务 ----------
async def scheduled_tasks(app):
    while True:
        try:
            # 1. 发送出勤到频道
            config = load_config()
            now = datetime.now(timezone.utc)
            for key, region in [("b", "湖州"), ("c", "嘉兴")]:
                ch_cfg = config[f"channel_{key}"]
                if not ch_cfg.get("enabled") or not ch_cfg.get("chat_id"):
                    continue
                interval = ch_cfg.get("interval_hours", 24)
                last_sent_str = config.get(f"last_sent_{key}")
                if last_sent_str:
                    try:
                        last_sent = datetime.fromisoformat(last_sent_str)
                    except:
                        last_sent = None
                else:
                    last_sent = None
                if not last_sent or (now - last_sent).total_seconds() >= interval * 3600:
                    await send_attendance_to_channel(app.bot, region, key)

            # 2. 个人有效期自动检查（不删除，只做提醒？但此处不额外处理，已在开课时检查）
            # 可选择每天发一次过期提醒，但暂不实现。
        except Exception as e:
            logger.error(f"定时任务异常: {e}")
        await asyncio.sleep(60)

# ---------- 每日重置任务 ----------
async def daily_reset_job():
    RESET_HOUR = 4
    RESET_MINUTE = 48
    while True:
        now_utc = datetime.now(timezone.utc)
        now_beijing = now_utc + timedelta(hours=8)
        target_beijing = now_beijing.replace(hour=RESET_HOUR, minute=RESET_MINUTE, second=0, microsecond=0)
        if now_beijing >= target_beijing:
            target_beijing += timedelta(days=1)
        target_utc = target_beijing - timedelta(hours=8)
        sleep_seconds = (target_utc - now_utc).total_seconds()
        logger.info(f"当前北京时间: {now_beijing.strftime('%Y-%m-%d %H:%M:%S')}, "
                    f"下次重置: {target_beijing.strftime('%Y-%m-%d %H:%M:%S')}, "
                    f"等待 {sleep_seconds/3600:.2f} 小时")
        await asyncio.sleep(sleep_seconds)
        reset_active_status()
        cleanup_inactive_users()

# ---------- 主程序 ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CallbackQueryHandler(admin_add_region_callback, pattern="^admin_add_region_"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, group_message_handler))
    app.add_handler(MessageHandler(filters.ALL, auto_heart_reaction), group=-1)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.create_task(daily_reset_job())
    loop.create_task(scheduled_tasks(app))

    logger.info("机器人已启动（数据持久化目录: %s）", DATA_DIR)
    logger.info(f"开课群: {GROUP_A_CHAT_ID} | 湖州群: {GROUP_B_CHAT_ID} | 嘉兴群: {GROUP_C_CHAT_ID}")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
