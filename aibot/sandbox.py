# -*- coding: UTF-8 -*-
"""bubblewrap 沙箱：给模型一个带持久目录的 shell

/workspace/user  当前用户的持久目录（HOME），跨聊天共享
/workspace/chat  当前聊天的持久目录（工作目录），聊天内共享

宿主机只读挂载 /usr 等系统目录，Bot 自身目录、配置与数据库都不可见。
所有文件读写都在沙箱内完成，避免沙箱里创建的符号链接影响宿主机。
"""

import os
import re
import shutil
import posixpath
import asyncio
from loguru import logger
from .config import config

available = False
reason = '尚未检测'

_lock = asyncio.Semaphore(4)
_ETC = ['/etc/resolv.conf', '/etc/hosts', '/etc/ssl', '/etc/ca-certificates', '/etc/pki', '/etc/alternatives',
        '/etc/ld.so.cache', '/etc/ld.so.conf', '/etc/ld.so.conf.d', '/etc/localtime', '/etc/nsswitch.conf',
        '/etc/mime.types', '/etc/protocols', '/etc/services']
_PATH = '/workspace/user/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def settings() -> dict:
    return config['AI']['Tool']['Shell']


def enabled() -> bool:
    return available and config['AI']['Tool']['Enable'] and settings()['Enable']


def root() -> str:
    return os.path.abspath(settings()['WorkDir'])


def _safe(name) -> str:
    return re.sub(r'[^0-9A-Za-z_.-]', '_', str(name))


def dirs(user_id, conv) -> tuple[str, str]:
    base = root()
    user_dir = os.path.join(base, 'users', _safe(user_id))
    chat_dir = os.path.join(base, 'chats', _safe(conv))
    os.makedirs(user_dir, exist_ok=True)
    os.makedirs(chat_dir, exist_ok=True)
    etc = os.path.join(base, '.etc')
    if not os.path.exists(os.path.join(etc, 'group')):
        os.makedirs(etc, exist_ok=True)
        with open(os.path.join(etc, 'passwd'), 'w') as fp:
            fp.write('root:x:0:0:root:/root:/bin/sh\nsandbox:x:1000:1000:sandbox:/workspace/user:/bin/bash\n')
        with open(os.path.join(etc, 'group'), 'w') as fp:
            fp.write('root:x:0:\nsandbox:x:1000:\n')
    return user_dir, chat_dir


def _args(user_dir: str, chat_dir: str) -> list[str]:
    args = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--uid', '1000', '--gid', '1000', '--hostname', 'sandbox']
    if settings()['Network']:
        args.append('--share-net')
    args += ['--ro-bind', '/usr', '/usr']
    for d in ('/bin', '/sbin', '/lib', '/lib32', '/lib64', '/libx32'):
        if os.path.islink(d):
            args += ['--symlink', os.readlink(d), d]
        elif os.path.isdir(d):
            args += ['--ro-bind', d, d]
    for p in _ETC:
        args += ['--ro-bind-try', p, p]
    etc = os.path.join(root(), '.etc')
    args += ['--ro-bind', os.path.join(etc, 'passwd'), '/etc/passwd', '--ro-bind', os.path.join(etc, 'group'), '/etc/group',
             '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--bind', user_dir, '/workspace/user', '--bind', chat_dir, '/workspace/chat', '--remount-ro', '/', '--chdir', '/workspace/chat',
             '--clearenv', '--setenv', 'HOME', '/workspace/user', '--setenv', 'USER', 'sandbox', '--setenv', 'PATH', _PATH,
             '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'TERM', 'dumb', '--setenv', 'PYTHONUNBUFFERED', '1']
    return args


async def _exec(user_id, conv, argv: list[str], stdin: bytes = None, timeout: float = None, merge=True, cap=512 * 1024):
    """运行并返回 (退出码, 输出, 错误输出, 是否超时)；输出超过 cap 时只保留首尾"""
    user_dir, chat_dir = dirs(user_id, conv)
    timeout = timeout or settings()['Timeout']
    async with _lock:
        proc = await asyncio.create_subprocess_exec(
            *_args(user_dir, chat_dir), *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge else asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def read(stream):
            head, tail, total = bytearray(), bytearray(), 0
            while chunk := await stream.read(65536):
                total += len(chunk)
                room = cap - len(head)
                if room > 0:
                    head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    tail = (tail + chunk)[-(cap // 2):]
            return bytes(head), bytes(tail), total

        async def feed():
            if stdin is None:
                return
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            proc.stdin.close()

        async def communicate():
            readers = [read(proc.stdout)] + ([] if merge else [read(proc.stderr)])
            results = await asyncio.gather(feed(), *readers)
            await proc.wait()
            return results[1:]

        timed_out = False
        try:
            results = await asyncio.wait_for(communicate(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
            results = [(b'', b'', 0)] * (1 if merge else 2)
    out = results[0]
    err = results[1] if not merge else (b'', b'', 0)
    return proc.returncode, out, err, timed_out


def resolve(path: str) -> str | None:
    """把模型给的路径规范到 /workspace 下，越界返回 None"""
    path = posixpath.normpath(posixpath.join('/workspace/chat', path.strip()))
    return path if path.startswith(('/workspace/chat/', '/workspace/user/')) else None


def _text(part) -> str:
    head, tail, total = part
    text = head.decode('utf-8', errors='replace')
    if total > len(head):
        text += f"\n...[省略 {total - len(head) - len(tail)} 字节]...\n" + tail.decode('utf-8', errors='replace')
    return text


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(limit - 60, 0)
    return text[:keep * 3 // 5] + f"\n...[省略 {len(text) - keep} 字符]...\n" + text[-(keep * 2 // 5):]


async def shell(user_id, conv, command: str, timeout: int = None) -> str:
    code, out, _, timed_out = await _exec(user_id, conv, ['/bin/bash', '-c', command], timeout=timeout)
    text = clip(_text(out), settings()['MaxOutput'])
    if timed_out:
        return f"[命令超时，已终止 (>{timeout or settings()['Timeout']}s)]\n{text}"
    return f"[exit code: {code}]\n{text}" if text else f"[exit code: {code}] (无输出)"


async def write_file(user_id, conv, path: str, data: bytes) -> str | None:
    """写入文件，成功返回 None，失败返回错误信息"""
    if not (path := resolve(path)):
        return '只能写入 /workspace/chat 或 /workspace/user 下的文件'
    script = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
    code, out, _, timed_out = await _exec(user_id, conv, ['/bin/sh', '-c', script, 'sh', path], stdin=data, timeout=max(settings()['Timeout'], 30))
    if timed_out:
        return '写入超时'
    if code != 0:
        return _text(out).strip() or f'exit code {code}'
    return None


async def read_file(user_id, conv, path: str, max_bytes: int) -> tuple[bytes | None, str]:
    """读取文件，返回 (内容, 错误信息)"""
    if not (path := resolve(path)):
        return None, '只能读取 /workspace/chat 或 /workspace/user 下的文件'
    script = 'test -f "$1" || { echo "文件不存在: $1" >&2; exit 2; }; head -c "$2" -- "$1"'
    code, out, err, timed_out = await _exec(user_id, conv, ['/bin/sh', '-c', script, 'sh', path, str(max_bytes + 1)],
                                            timeout=max(settings()['Timeout'], 30), merge=False, cap=max_bytes + 1)
    if timed_out:
        return None, '读取超时'
    if code != 0:
        return None, _text(err).strip() or f'exit code {code}'
    data = out[0]
    if out[2] > max_bytes:
        return None, f'文件超过 {max_bytes // 1024 // 1024} MB'
    return data, ''


async def probe():
    """检测 bwrap 是否可用（容器内需要放开 seccomp / apparmor / systempaths）"""
    global available, reason
    if not shutil.which('bwrap'):
        available, reason = False, '未安装 bubblewrap (apt install bubblewrap)'
    else:
        try:
            code, out, _, _ = await _exec('probe', 'probe', ['/bin/sh', '-c', 'echo ok'], timeout=10)
            text = _text(out).strip()
            available, reason = (code == 0 and text == 'ok'), text
        except Exception as e:
            available, reason = False, str(e)
        shutil.rmtree(os.path.join(root(), 'users', 'probe'), ignore_errors=True)
        shutil.rmtree(os.path.join(root(), 'chats', 'probe'), ignore_errors=True)
    if available:
        reason = ''
        logger.info('Sandbox ready (bubblewrap)')
    else:
        if os.path.exists('/.dockerenv'):
            reason += ' (Docker 需要 security_opt: seccomp=unconfined, apparmor=unconfined, systempaths=unconfined)'
        logger.warning(f'Sandbox unavailable, shell tool disabled: {reason}')
