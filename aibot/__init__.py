# -*- coding: UTF-8 -*-

VERSION = "2.0.0"
VERSION_ID = 2026092401
VERSION_SOURCE = "Official"

# 版本更新介绍：启动时会把比 Other.CurrentVersion 新的条目发给超管
CHANGELOG = {
    2026092401: (
        "- 沙箱换成 bubblewrap：模型可以使用 shell，并拥有按用户 / 按聊天划分的持久目录\n"
        "- 新增 `send_file` 工具，模型可以把工作目录中的文件发回聊天\n"
        "- 记忆优化：滚动摘要、长期记忆工具、历史全文检索，数据库单表存储并开启 WAL\n"
        "- 新增 `/get`、`/set`、`/config` 配置面板与配置热重载\n"
        "- 新增 `/memory` 查看或清除长期记忆\n"
        "- 普通、访客、内联三种对话入口共用同一个对话引擎"
    ),
}
