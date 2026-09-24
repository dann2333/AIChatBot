# -*- coding: UTF-8 -*-
"""配置读写：/get /set 与 /config 可视化面板"""

import yaml
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from .config import config, ConfigError
from .chat import rand_id

MASK = '******'
INT_LISTS = {'SuAdmin', 'Admin', 'AI.WhiteList'}
sessions = {}    # token -> {'uid': int, 'path': str}
pending = {}     # (chat_id, uid) -> {'token': str, 'mode': 'e' | 'a', 'panel': (chat_id, msg_id) | inline_message_id}


def show(path: str, value) -> str:
    if config.is_hidden(path):
        return MASK if value not in ('', None, 0) else '(空)'
    if isinstance(value, (dict, list)):
        text = yaml.safe_dump(mask(path, value), allow_unicode=True, sort_keys=False).strip()
        return text if len(text) < 1500 else text[:1500] + '\n...'
    return repr(value) if isinstance(value, str) else str(value)


def mask(path: str, value):
    """dict/list 中的敏感字段打码"""
    if isinstance(value, dict):
        return {k: mask(f"{path}.{k}" if path != '.' else str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(path, v) for v in value]
    return MASK if config.is_hidden(path) and value not in ('', None) else value


def type_of(path: str, value) -> str:
    desc = config.describe(path)
    return desc['type'] if desc else type(value).__name__


# ---------- /get /set ----------

def cmd_get(path: str) -> str:
    path = path.strip() or '.'
    try:
        value = config.get(path)
    except KeyError:
        return f"❌ 配置项 `{path}` 不存在哦 ~"
    desc = config.describe(path)
    return f"✔️ 读取配置成功啦 ~\n\n描述: `{desc['Description'] if desc else '无'}`\n键: `{path}`\n值:\n```\n{show(path, value)}\n```"


def cmd_set(path: str, text: str) -> tuple[bool, str]:
    try:
        old = config.get(path)
    except KeyError:
        old = None
    try:
        config.set(path, config.parse(path, text))
    except ConfigError as e:
        return False, f"❌ 修改失败: {e}"
    return True, f"✔️ 修改配置成功啦 ~\n\n键: `{path}`\n修改前: `{show(path, old)}`\n修改后: `{show(path, config.get(path))}`"


# ---------- /config 面板 ----------

def open_session(uid: int, path: str = '.') -> str:
    token = rand_id(6)
    sessions[token] = {'uid': uid, 'path': path}
    while len(sessions) > 200:
        sessions.pop(next(iter(sessions)))
    return token


def children(path: str, value) -> list:
    desc = config.describe(path)
    if desc and desc['Branch']:
        return desc['Branch']
    return list(value) if isinstance(value, dict) else []


def child_path(path: str, key) -> str:
    return str(key) if path == '.' else f"{path}.{key}"


def render(token: str, note: str = '') -> tuple[str, InlineKeyboardMarkup]:
    s = sessions[token]
    path = s['path']
    try:
        value = config.get(path)
    except KeyError:
        s['path'] = path = '.'
        value = config.get(path)
    desc = config.describe(path)
    type_ = type_of(path, value)
    lines = [f"⚙️ 配置面板 `{path}`"]
    if desc:
        lines.append(f"描述: {desc['Description']}")
    lines.append(f"类型: `{type_}`")
    rows = []
    btn = lambda text, action, arg='': InlineKeyboardButton(text, callback_data=f"cfg {token} {action} {arg}".strip())
    keys = children(path, value)
    if keys:
        buttons = [btn(str(k), 'o', i) for i, k in enumerate(keys[:40])]
        rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        if not (desc and desc['Branch']):
            lines.append(f"\n```\n{show(path, value)}\n```")
    elif isinstance(value, list):
        lines.append("\n" + ("\n".join(f"{i}. `{show(path, v)}`" for i, v in enumerate(value)) or "(空列表)"))
        dels = [btn(f"删除 {i}", 'd', i) for i in range(min(len(value), 20))]
        rows += [dels[i:i + 4] for i in range(0, len(dels), 4)]
        rows.append([btn('添加', 'a')])
    elif type_ == 'bool':
        lines.append(f"值: `{value}`")
        rows.append([btn(f"切换为 {not value}", 't')])
    else:
        lines.append(f"值: `{show(path, value)}`")
        rows.append([btn('修改', 'e')])
    if path != '.':
        tail = [btn('返回上级', 'u')]
        if desc:
            tail.insert(0, btn('恢复默认', 'r'))
        rows.append(tail)
    rows.append([btn('关闭', 'x')])
    if note:
        lines.append(f"\n{note}")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_callback(client, cq, parts: list, refresh):
    """处理 cfg <token> <action> [arg]"""
    token, action = parts[1], parts[2]
    arg = parts[3] if len(parts) > 3 else ''
    s = sessions.get(token)
    if s is None:
        return await cq.answer("❌ 面板已过期，请重新发送 /config ~", show_alert=True)
    if cq.from_user.id != s['uid']:
        return await cq.answer("❌ 请不要点击别人的按钮哦 ~", show_alert=True)
    path, note = s['path'], ''
    try:
        if action == 'x':
            pending.pop((cq.message.chat.id if cq.message else None, s['uid']), None)
            sessions.pop(token, None)
            return await (cq.message.delete() if cq.message else cq.edit_message_text("✔ 面板已关闭 ~"))
        if action == 'o':
            keys = children(path, config.get(path))
            s['path'] = child_path(path, keys[int(arg)])
        elif action == 'u':
            s['path'] = path.rsplit('.', 1)[0] if '.' in path else '.'
        elif action == 't':
            config.set(path, not config.get(path))
            await refresh(client)
            note = '✔ 已切换'
        elif action == 'r':
            config.reset(path)
            await refresh(client)
            note = '✔ 已恢复默认值'
        elif action == 'd':
            value = list(config.get(path))
            value.pop(int(arg))
            config.set(path, value)
            await refresh(client)
            note = '✔ 已删除'
        elif action in ('e', 'a'):
            if not cq.message:
                return await cq.answer("❌ 内联消息中无法输入，请在聊天中使用 /config ~", show_alert=True)
            pending[(cq.message.chat.id, s['uid'])] = {'token': token, 'mode': action, 'panel': cq.message.id}
            note = '✏️ 请直接发送' + ('要添加的元素' if action == 'a' else '新的值') + '，发送 /cancel 取消（空格照常输入即可）'
    except (ConfigError, ValueError, IndexError, KeyError) as e:
        note = f"❌ 操作失败: {e}"
    text, markup = render(token, note)
    await cq.edit_message_text(text, reply_markup=markup)


async def on_input(client, message, refresh) -> bool:
    """面板等待输入时截获用户的下一条消息，返回是否已处理"""
    if not message.from_user:
        return False
    key = (message.chat.id, message.from_user.id)
    p = pending.get(key)
    if p is None or p['token'] not in sessions:
        return False
    pending.pop(key)
    text = message.text or message.caption or ''
    s = sessions[p['token']]
    try:
        await message.delete()
    except Exception:
        pass
    if text.strip() == '/cancel':
        note = '✔ 已取消'
    else:
        try:
            if p['mode'] == 'a':
                value = list(config.get(s['path']))
                value.append(int(text.strip()) if s['path'] in INT_LISTS else text)
                config.set(s['path'], value)
            else:
                config.set(s['path'], config.parse(s['path'], text.replace('\\', '\\\\')))
            await refresh(client)
            note = '✔ 修改成功'
        except (ConfigError, ValueError, KeyError) as e:
            note = f"❌ 修改失败: {e}"
    body, markup = render(p['token'], note)
    try:
        await client.edit_message_text(message.chat.id, p['panel'], body, reply_markup=markup)
    except Exception:
        pass
    return True
