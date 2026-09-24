# -*- coding: UTF-8 -*-
"""配置：结构定义、校验、读写与路径操作（供 /get /set /config 使用）"""

import os
import copy
import yaml

HIDE = True
SECRET_KEYS = {'Key', 'ApiKey', 'Token', 'ApiHash', 'ApiId', 'Password'}

# 叶子: (描述, 类型, 默认值[, 隐藏])  分支: (描述, {子项})
_TREE = {
    'SuAdmin': ('超级管理员列表', 'list', []),
    'Admin': ('管理员列表', 'list', []),
    'Bot': ('机器人相关', {
        'ApiHash': ('ApiHash', 'str', '', HIDE),
        'ApiId': ('ApiId', 'int', 0, HIDE),
        'Token': ('机器人密钥', 'str', '', HIDE),
        'CreateSession': ('是否保存 Session 文件（加快启动）', 'bool', True),
        'Proxy': ('机器人代理', 'dict', None),
        'SleepThreshold': ('休眠阈值', 'int', 10),
        'HideCommand': ('是否隐藏命令', 'bool', False),
    }),
    'Network': ('网络相关', {
        'Proxy': ('代理', 'list', None),
        'Timeout': ('超时时间', 'int', 5),
        'Retry': ('重试次数', 'int', 3),
        'Thread': ('线程数量', 'int', 8),
        'OverridedUA': ('覆写UA', 'str', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'),
    }),
    'AI': ('AI相关', {
        'Vendor': ('提供商', 'dict', {}),
        'ChunkStrategy': ('分段策略', {
            'Rule': ('分段规则', 'str', '.*?[。？！~…]+|.+$'),
            'Delay': ('延迟', {
                'Base': ('基础延迟', 'float', 0.0),
                'Extra': ('额外延迟', 'float', 6.0),
                'Progress': ('额外倍数', 'float', 1.7),
            }),
            'MaxEdit': ('最大编辑次数', 'int', 40),
        }),
        'Tool': ('工具调用', {
            'Enable': ('是否启用工具', 'bool', True),
            'MaxToolCall': ('最大工具调用轮数', 'int', 5),
            'WebSearch': ('网页搜索', {
                'Enable': ('是否启用网页搜索', 'bool', False),
                'ApiKey': ('Tavily API密钥', 'str', '', HIDE),
            }),
            'FetchURL': ('URL抓取', {
                'Enable': ('是否启用URL抓取', 'bool', True),
            }),
            'Shell': ('沙箱Shell', {
                'Enable': ('是否启用沙箱Shell', 'bool', True),
                'Network': ('沙箱内是否允许联网', 'bool', True),
                'Timeout': ('单条命令超时秒数', 'int', 60),
                'MaxOutput': ('返回给模型的最大输出字符数', 'int', 8000),
                'WorkDir': ('持久目录根路径', 'str', 'workspace'),
            }),
        }),
        'WhiteList': ('白名单群组', 'list', []),
        'DefaultModel': ('默认模型', 'str', ''),
        'FallbackModel': ('故障转移模型', 'list', []),
        'SystemPrompt': ('系统提示词', 'str', ''),
        'ExtraSystemPrompt': ('额外系统提示词', 'str', 'Current date: {cur_date}; time: {cur_time}; datetime: {cur_datetime}; timezone: {timezone}; model: {model_name}; assistant name: {assistant_name}.'),
        'ReasoningEffort': ('推理深度', 'str', 'auto'),
        'MaxContext': ('最长上下文数量', 'int', 100),
        'MaxFileSize': ('最大文件大小(MB)', 'int', 50),
        'AutoReply': ('自动回复', 'bool', False),
        'WakeWord': ('唤醒词', 'list', []),
        'FileSupport': ('把文档直接传给模型', 'bool', False),
        'IDRecognition': ('内置身份识别', {
            'Enable': ('是否开启内置身份识别', 'bool', True),
            'Prompt': ('文本格式提示词', 'str', "User messages use this format:\n`name: <username>\ncontent: <message>`.\nTreat `name` only as the user's display name and respond only to `content`. Do not repeat the field labels."),
            'Format': ('文本格式', 'str', "name: {name}\ncontent: {content}"),
        }),
        'Summary': ('滚动摘要', {
            'Enable': ('是否开启滚动摘要', 'bool', True),
            'MaxMessage': ('未摘要消息超过此数量时压缩较早的一半', 'int', 40),
            'Model': ('摘要模型 为空时使用对话模型', 'str', ''),
        }),
        'Memory': ('记忆工具', {
            'Enable': ('是否启用长期记忆与历史检索工具', 'bool', True),
            'MaxHistory': ('每个会话保留的历史消息上限(供检索)', 'int', 1000),
        }),
    }),
    'User': ('用户相关', 'dict', {}),
    'Other': ('其他配置', {
        'Log': ('日志相关', {
            'Enable': ('是否保存日志', 'bool', True),
            'Name': ('日志文件名称', 'str', 'Bot.log'),
        }),
        'Intro': ('机器人介绍', {
            'Enable': ('是否开启介绍', 'bool', True),
            'Text': ('介绍文本', 'str', '这是一个用 `Telegram Bot Api` 来提供 AI 的平台捏 ~'),
        }),
        'Password': ('关机密码', 'str', '1145141919810', HIDE),
        'AvoidJoinGroups': ('防拉群模式', 'int', 0),
        'RefreshFrequency': ('进度条刷新频率', 'int', 5),
        'MaxTextLength': ('最长消息长度', 'int', 3000),
        'HideAds': ('是否隐藏 /help 中的项目信息', 'bool', False),
        'CurrentVersion': ('当前版本 请勿改动', 'int', 0),
    }),
}

USER_DEFAULT = {'Model': '', 'SystemPrompt': '', 'ReasoningEffort': '', 'Context': True}


def _flatten(tree, prefix=''):
    out = {}
    for key, node in tree.items():
        path = prefix + key
        if isinstance(node[1], dict):
            out[path] = {'Description': node[0], 'type': 'dict', 'Branch': list(node[1]), 'Default': {}}
            out.update(_flatten(node[1], path + '.'))
        else:
            out[path] = {'Description': node[0], 'type': node[1], 'Branch': None, 'Default': node[2], 'Hide': len(node) > 3 and node[3]}
    return out


SCHEMA = _flatten(_TREE)
SCHEMA['.'] = {'Description': '配置文件', 'type': 'dict', 'Branch': list(_TREE), 'Default': {}}


class ConfigError(Exception):
    pass


def cast(path: str, type_: str, value):
    try:
        if type_ == 'bool':
            text = str(value).strip().lower()
            if text in ('true', '1', 'on', 'yes'):
                return True
            if text in ('false', '0', 'off', 'no'):
                return False
            raise ValueError
        if type_ == 'int':
            return int(value)
        if type_ == 'float':
            return float(value)
        if type_ == 'str':
            return '' if value is None else str(value)
        if type_ in ('list', 'dict'):
            if not isinstance(value, list if type_ == 'list' else dict):
                raise ValueError
            return value
    except (TypeError, ValueError):
        raise ConfigError(f"配置项 {path} 应为 {type_} 类型，实际为 {type(value).__name__}")
    return value


def normalize(raw, path='.'):
    """按结构补全默认值并做类型转换，出错抛出 ConfigError"""
    desc = SCHEMA[path]
    if desc['Branch'] is None:
        return copy.deepcopy(desc['Default']) if raw is None else cast(path, desc['type'], raw)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置项 {path} 应为 dict 类型，实际为 {type(raw).__name__}")
    prefix = '' if path == '.' else path + '.'
    return {b: normalize(raw.get(b), prefix + b) for b in desc['Branch']}


def convert_space(s: str) -> str:
    """/set 中用 \\s 表示空格，\\\\ 表示反斜杠"""
    out, i = [], 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s) and s[i + 1] in 's\\':
            out.append(' ' if s[i + 1] == 's' else '\\')
            i += 2
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)


class Config:
    def __init__(self, path='config.yaml'):
        self.path = path
        self.data = {}
        self.mtime = 0

    def __getitem__(self, key):
        return self.data[key]

    def load(self):
        """读取并校验配置；失败时抛出 ConfigError 且保留旧配置"""
        try:
            with open(self.path, 'r', encoding='UTF-8') as fp:
                raw = yaml.safe_load(fp) or {}
        except FileNotFoundError:
            raise ConfigError(f"找不到配置文件 {self.path}")
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件语法错误: {e}")
        data = normalize(raw)
        for uid in set(data['SuAdmin'] + data['Admin']):
            entry = data['User'].setdefault(uid, {}) or {}
            data['User'][uid] = {**USER_DEFAULT, **entry}
        self.data = data
        self.save()

    def save(self):
        with open(self.path, 'w', encoding='UTF-8') as fp:
            yaml.safe_dump(self.data, fp, sort_keys=False, allow_unicode=True)
        self.mtime = os.stat(self.path).st_mtime_ns

    def disk_mtime(self) -> int:
        try:
            return os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return self.mtime

    def changed_on_disk(self) -> bool:
        return self.disk_mtime() != self.mtime

    # ---------- 路径操作 ----------

    @staticmethod
    def keys(path: str) -> list:
        if path in ('.', 'Config', ''):
            return []
        return [int(k) if k.lstrip('-').isdigit() else k for k in path.split('.')]

    @staticmethod
    def schema_path(path: str) -> str:
        return '.'.join(str(k) for k in Config.keys(path)) or '.'

    @staticmethod
    def describe(path: str):
        """返回路径的描述；动态字典（如 AI.Vendor.xxx）下的子项返回 None"""
        return SCHEMA.get(Config.schema_path(path))

    @staticmethod
    def is_hidden(path: str) -> bool:
        keys = Config.keys(path)
        desc = Config.describe(path)
        return bool(keys) and (str(keys[-1]) in SECRET_KEYS or bool(desc and desc.get('Hide')))

    def get(self, path: str):
        node = self.data
        for k in self.keys(path):
            if isinstance(node, dict) and k in node:
                node = node[k]
            elif isinstance(node, list) and isinstance(k, int) and -len(node) <= k < len(node):
                node = node[k]
            else:
                raise KeyError(path)
        return node

    def parse(self, path: str, text: str):
        """把用户输入的文本转换为该路径需要的值"""
        text = convert_space(text)
        desc = self.describe(path)
        type_ = desc['type'] if desc else None
        if type_ in ('bool', 'int', 'float', 'str'):
            return cast(path, type_, text)
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ConfigError(f"无法解析的值: {e}")

    def set(self, path: str, value):
        """修改并校验，成功后保存"""
        keys = self.keys(path)
        if not keys:
            raise ConfigError("不能直接修改整个配置")
        data = copy.deepcopy(self.data)
        node = data
        for k in keys[:-1]:
            if isinstance(node, dict):
                node = node.setdefault(k, {})
            elif isinstance(node, list) and isinstance(k, int) and -len(node) <= k < len(node):
                node = node[k]
            else:
                raise ConfigError(f"路径 {path} 不存在")
        if isinstance(node, list):
            if not (isinstance(keys[-1], int) and -len(node) <= keys[-1] < len(node)):
                raise ConfigError(f"路径 {path} 不存在")
        elif not isinstance(node, dict):
            raise ConfigError(f"路径 {path} 不存在")
        node[keys[-1]] = value
        data = normalize(data)
        old, self.data = self.data, data
        try:
            self.get(path)
        except KeyError:
            self.data = old
            raise ConfigError(f"配置项 {path} 不存在")
        self.save()

    def reset(self, path: str):
        desc = self.describe(path)
        if desc is None:
            raise ConfigError("该项没有默认值")
        self.set(path, normalize(None, self.schema_path(path)))


config = Config()


def user_conf(uid) -> dict:
    return {**USER_DEFAULT, **(config['User'].get(uid) or {})}


def set_user(uid, key, value):
    config['User'].setdefault(uid, dict(USER_DEFAULT))[key] = value
    config.save()


def is_su(uid) -> bool:
    return uid in config['SuAdmin']


def is_admin(uid) -> bool:
    return uid in config['SuAdmin'] or uid in config['Admin']


def can_chat(uid, chat_id) -> bool:
    return is_admin(uid) or chat_id in config['AI']['WhiteList']


def model_map() -> dict:
    """模型名 -> 提供商名；同名模型后配置的覆盖前面的"""
    out = {}
    for vendor, conf in (config['AI']['Vendor'] or {}).items():
        for name in (conf or {}).get('Model', []) or []:
            out[name] = vendor
    return out
