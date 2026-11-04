"""给智能体用的本机工具集。

原来是一个 900 多行的单文件，现在按领域拆开：

    core.py      路径守卫、目录/文件、终端、记忆、日志
    selfdev.py   自我优化：读结构、搜自己的源码、改完自动校验
    edits.py     改动记录：编辑工具一执行就落盘，给模型看「我到底改了什么」

这里统一再导出一次 —— 外部 `import agent_tools` 之后
`agent_tools.read_file(...)` 这种写法完全不用改。

edits 是个模块而不是 `*` 导出：它的函数名（record / read_index）
太通用，摊到 agent_tools 顶层容易和别处撞名，所以留着 `edits.xxx` 调用。
"""

from .core import *          # noqa: F401,F403
from .selfdev import *       # noqa: F401,F403
from . import edits          # noqa: F401

# 单独再导一次：`import *` 不带下划线开头的名字，但 agent_actions 写盘时要
# 按「读的时候用的那个编码」写回去（BOM / GBK 才不会被抹掉），判据必须和
# read_file 共用同一份，不能再手写一遍。
from .core import _decode_enc  # noqa: F401
