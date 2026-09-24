# -*- coding: UTF-8 -*-
"""SQLite 存储：对话历史（含全文检索）、滚动摘要、长期记忆"""

import json
import time
import aiosqlite
from loguru import logger

db: aiosqlite.Connection = None
fts = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT,
    content TEXT NOT NULL,
    summarized INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv, id);
CREATE TABLE IF NOT EXISTS summaries (
    conv TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    scope TEXT NOT NULL,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    updated INTEGER NOT NULL,
    PRIMARY KEY (scope, topic)
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id', tokenize='{tokenizer}');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
"""

KNOWN_TABLES = {'messages', 'summaries', 'memories', 'sqlite_sequence'}


async def init(path='Chat.db'):
    global db, fts
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.executescript(SCHEMA)
    # trigram 分词可检索中文子串，老版本 SQLite 退回默认分词，没有 FTS5 时用 LIKE
    for tokenizer in ('trigram', 'unicode61'):
        try:
            await db.executescript(FTS_SCHEMA.format(tokenizer=tokenizer))
            fts = tokenizer == 'trigram'
            break
        except Exception:
            continue
    await db.commit()
    await migrate_legacy()


async def close():
    if db:
        await db.close()


def slim(content) -> str:
    """把多模态内容转成纯文本，图片与文件只保留占位说明"""
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if item.get('type') == 'text':
            parts.append(item.get('text', ''))
        elif item.get('type') == 'image_url':
            parts.append('[图片]')
        elif item.get('type') == 'file':
            parts.append(f"[文件: {item.get('file', {}).get('filename', 'file')}]")
    return '\n'.join(p for p in parts if p)


async def migrate_legacy():
    """旧版本每个聊天一张表 (name, content)，迁移到 messages 后删除"""
    async with db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
        tables = [r['name'] for r in await cur.fetchall()]
    migrated = 0
    for table in tables:
        if table in KNOWN_TABLES or table.startswith('messages_fts'):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            async with db.execute(f"SELECT name, content FROM {quoted} ORDER BY rowid") as cur:
                rows = await cur.fetchall()
        except Exception:
            continue
        now = int(time.time())
        for row in rows:
            content = row['content']
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    content = slim(parsed)
            except Exception:
                pass
            role, name = (row['name'], None) if row['name'] in ('user', 'assistant') else ('user', row['name'])
            await db.execute("INSERT INTO messages (conv, role, name, content, created) VALUES (?, ?, ?, ?, ?)", (table, role, name, content, now))
        await db.execute(f"DROP TABLE {quoted}")
        migrated += 1
    if migrated:
        await db.commit()
        await db.execute("VACUUM")
        logger.info(f"Migrated {migrated} legacy chat tables")


# ---------- 对话历史 ----------

async def add_message(conv: str, role: str, content: str, name: str = None, keep: int = 1000):
    await db.execute("INSERT INTO messages (conv, role, name, content, created) VALUES (?, ?, ?, ?, ?)", (conv, role, name, content, int(time.time())))
    await db.execute("""
        DELETE FROM messages WHERE conv = ? AND id <= (
            SELECT id FROM messages WHERE conv = ? ORDER BY id DESC LIMIT 1 OFFSET ?
        )""", (conv, conv, max(keep, 1)))
    await db.commit()


async def recent(conv: str, limit: int, unsummarized_only: bool = False) -> list[dict]:
    sql = "SELECT id, role, name, content FROM messages WHERE conv = ?"
    if unsummarized_only:
        sql += " AND summarized = 0"
    async with db.execute(f"SELECT * FROM ({sql} ORDER BY id DESC LIMIT ?) ORDER BY id", (conv, max(limit, 0))) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def count(conv: str, unsummarized_only: bool = False) -> int:
    sql = "SELECT COUNT(*) FROM messages WHERE conv = ?" + (" AND summarized = 0" if unsummarized_only else "")
    async with db.execute(sql, (conv,)) as cur:
        return (await cur.fetchone())[0]


async def clear(conv: str):
    await db.execute("DELETE FROM messages WHERE conv = ?", (conv,))
    await db.execute("DELETE FROM summaries WHERE conv = ?", (conv,))
    await db.commit()


async def search(conv: str, query: str, limit: int = 8) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    # trigram 至少需要 3 个字符，短词退回 LIKE
    terms = [t for t in query.split() if len(t) >= 3]
    if fts and terms:
        sql = """SELECT m.id, m.role, m.name, m.content, m.created FROM messages_fts f
                 JOIN messages m ON m.id = f.rowid
                 WHERE messages_fts MATCH ? AND m.conv = ? ORDER BY m.id DESC LIMIT ?"""
        args = (' OR '.join('"' + t.replace('"', '""') + '"' for t in terms), conv, limit)
    else:
        sql = "SELECT id, role, name, content, created FROM messages WHERE conv = ? AND content LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?"
        like = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        args = (conv, like, limit)
    async with db.execute(sql, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ---------- 滚动摘要 ----------

async def get_summary(conv: str) -> str:
    async with db.execute("SELECT content FROM summaries WHERE conv = ?", (conv,)) as cur:
        row = await cur.fetchone()
    return row['content'] if row else ''


async def set_summary(conv: str, content: str, upto_id: int):
    await db.execute("INSERT INTO summaries (conv, content, updated) VALUES (?, ?, ?) ON CONFLICT(conv) DO UPDATE SET content = excluded.content, updated = excluded.updated", (conv, content, int(time.time())))
    await db.execute("UPDATE messages SET summarized = 1 WHERE conv = ? AND id <= ?", (conv, upto_id))
    await db.commit()


# ---------- 长期记忆 ----------

async def save_memory(scope: str, topic: str, content: str):
    await db.execute("INSERT INTO memories (scope, topic, content, updated) VALUES (?, ?, ?, ?) ON CONFLICT(scope, topic) DO UPDATE SET content = excluded.content, updated = excluded.updated", (scope, topic, content, int(time.time())))
    await db.commit()


async def delete_memory(scope: str, topic: str) -> bool:
    cur = await db.execute("DELETE FROM memories WHERE scope = ? AND topic = ?", (scope, topic))
    await db.commit()
    return cur.rowcount > 0


async def list_memories(scope: str) -> list[dict]:
    async with db.execute("SELECT topic, content FROM memories WHERE scope = ? ORDER BY updated", (scope,)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def clear_memories(scope: str):
    await db.execute("DELETE FROM memories WHERE scope = ?", (scope,))
    await db.commit()
