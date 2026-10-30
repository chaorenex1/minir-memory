# /adviser 的命令归属：伴随插件（方案 A）

状态：设计已选定并已在 2026-10-28 落地（`plugin/minir-adviser` 已部署、宿主已启用）；本文是
该改动的设计记录与验收口径。未决项：重启后活网关里 `/adviser multagents` 是否走完
"排队 → 注入一轮 → 该轮 arm + 加载技能" 尚未验证（见"验收"第 4 条）；当前网关进程加载的 minir
模块早于本次新增的 verb/kickoff seam，因此活探针只走到 NOT_READY。

## 1. 症状与根因

症状：CLI 对 `/adviser` 回 `未知命令 /adviser — 未发送任何内容。`，而代码、测试、部署字节都没问题。

根因（实测，非推断）：

- 宿主把带 `register_memory_provider` 的插件判为 exclusive，插件发现阶段只登记不加载：
  `_plugins['minir']` → `enabled=False`、`module=None`、
  `error="exclusive plugin — activate via <category>.provider config"`。
- 因此 `plugin/minir/__init__.py:register(ctx)` 只在 provider 加载时才跑，而那发生在**第一个 agent turn**。
- 命令原本注册在这个 `register()` 里（旧代码 `ctx.register_command("adviser", ...)`），于是从进程启动到
  第一个 turn 之间 `/adviser` 不在任何命令表里，`cli.py::_expand_slash_prefix` 就打了 unknown。

同进程探针的差值说明了窗口：

```
discovery only:      ['disk-cleanup', 'quota']
after provider load: ['adviser', 'disk-cleanup', 'quota']
```

## 2. 方案 A

**把命令的注册搬到 discovery 能看见的地方，把状态留在 provider 上。**

- 新目录 `plugin/minir-adviser/`：普通插件（manifest 里不调用 `register_memory_provider`），
  discovery 阶段加载，注册 `/adviser`，自己不持有任何模式状态。
- `minir`（provider）继续持有 `adviser_command.ModeSwitch`（`provider._modes`），`pre_llm_call`
  读它注入 directive。
- 两者通过一个**具名 seam** 连接：两个插件目录是各自独立的 package，无法互相 import，
  所以伴随插件扫描 `sys.modules` 找 `_minir_adviser_command` 并按名字+签名调用。
- 命令解析、字符串、注入 kickoff 全部留在 `adviser_command`（单一实现）；伴随插件只做转发并
  提供自己的 `inject_message`。
- provider 尚未初始化时（正是方案 A 要关的那个窗口），命令不直接拒绝：按会话记下请求
  （`PENDING`），用 `inject_message` 注入一条 ready-check 请求——那一轮构建 agent 时 provider 才
  初始化，伴随插件的 `pre_llm_call` 钩子把排队的请求打到刚可用的开关上，minir 同一轮的 directive
  让模型加载 `minir:hermes-lead`、确认 rubric 后停下；下一条消息就是用户的需求。
  为此 minir 另外公开两个无状态 seam：`_minir_adviser_verb`（arm/clear/status/unknown，词表仍只在
  `adviser_command.parse`）与 `_minir_adviser_kickoff`（注入文本）。

取舍：多一个插件目录、一份部署目标、一处跨目录契约；换来命令从进程启动就存在、且只有一处注册点。

## 3. 代码

### 3.1 seam（minir 侧）

```python
# file: plugin/minir/__init__.py  （放在 _resolve_provider 之后）

def _minir_adviser_command(raw_args: str, *, session_id: str = "",
                           deliver: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """Cross-plugin seam for the ``minir-adviser`` plugin's ``/adviser`` command.

    返回要打印的那一行，或在没有任何 provider 初始化时返回 None —— 开关在 provider 上，
    第一个 turn 之前没有可切换的状态，由调用方自己回答。
    """
    provider = _ACTIVE_PROVIDER
    if provider is None or getattr(provider, "_store", None) is None:
        return None
    return adviser_command.command_handler(lambda: provider, inject=deliver)(
        raw_args, session_id=session_id)
```

同时 `register()` 里原来的 `ctx.register_command("adviser", ...)` 整块删除——命令只有一个归属。

### 3.2 伴随插件

```yaml
# file: plugin/minir-adviser/plugin.yaml
name: minir-adviser
version: 0.1.0
description: "Registers /adviser where plugin discovery can see it: the minir memory provider owns the mode switch, but it imports only at the first turn."
author: chaorenex1
license: MIT
```

```python
# file: plugin/minir-adviser/__init__.py

SEAM = "_minir_adviser_command"
NOT_READY = ("adviser: the minir memory provider is not loaded in this process yet; the mode "
             "switch lives on it and it loads with the first turn. Send a message, then rerun /adviser.")

def _seams():
    """每个已加载模块上发布 seam 的函数。两份是常态：provider 与 context engine 各导入一次 minir，
    只有初始化过的那份会给出答案。"""
    found = []
    for module in list(sys.modules.values()):
        seam = getattr(module, SEAM, None)
        if callable(seam):
            found.append(seam)
    return found

def run_adviser(raw_args, *, session_id="", deliver=None):
    for seam in _seams():
        try:
            answer = seam(raw_args, session_id=session_id, deliver=deliver)
        except Exception:                    # 坏掉的 seam 不能带走命令面
            logger.warning("minir adviser seam failed", exc_info=True)
            continue
        if answer is not None:
            return answer
    return None

def register(ctx):
    inject = getattr(ctx, "inject_message", None)
    deliver = (lambda text: bool(inject(text))) if callable(inject) else None

    def command(raw_args="", session_id="", session_key="", **kwargs):
        answer = run_adviser(raw_args, session_id=session_id or session_key, deliver=deliver)
        return answer if answer is not None else NOT_READY

    ctx.register_command("adviser", command,
                         description="Switch this session between hermes-dispatch and hermes-lead mode.",
                         args_hint="multagents | dispatch | status")
```

`run_adviser` 用 `None` 表示"没人回答"，`command` 把它翻成 `NOT_READY`；`NOT_READY` 对任何参数都为真
（包括 `status`），不谎称切了模式。

### 3.3 部署

```python
# file: scripts/install.py
PLUGIN_DIRS = ("minir", "minir-adviser")     # 默认两个目录一起部署
```

## 4. 验收

1. `hermes plugins validate plugin/minir-adviser` → Validation passed。
2. 干净进程的发现探针（`hermes-plugin-development/scripts/probe_plugin_commands.py`）：
   `discovery only` 必须已含 `adviser`，且 `added by provider load: []`。
3. 活网关：`commands.catalog` 含 `adviser`；`slash.exec` 发 `/adviser status` 回
   `{"type": "exec", ...}` 而不是 `unsupported_command`。
4. **重启后** `/adviser status` 回 `adviser: mode=dispatch (hermes-dispatch governs).`
   ——这一条还没验证：17:55 的 `hermes plugins enable` 重载过网关，运行中的 agent 仍持它构建时的
   provider 实例，重载后新 minir 模块的 `_ACTIVE_PROVIDER` 为空，活探针因此拿到的是一行 NOT_READY。
5. 单测：`tests/test_minir_adviser_command.py`（注册与钩子、转发、session/session_key、坏 seam
   兜底、provider 未加载时的排队+注入、status/dispatch/unknown 的分派、flush 在有/无 provider 两种
   情况下的行为、真实 provider 的 arm+kickoff）+ `tests/test_adviser_command.py`（开关与 directive）。
6. provider 未加载的整条链（排队 → 注入 → flush arm → directive）由第 5 项的单测覆盖，不依赖网关；
   活网关因为加载的是旧 minir 模块（缺 verb/kickoff seam），只能验到 `_verb` 返回空 → NOT_READY。

若第 4 条在重启后仍失败，下一个诊断是模块身份：在活进程里打印 `id(sys.modules[...])` 与
`_ACTIVE_PROVIDER` 所在模块的 `id`，确认 reload 是否换了模块对象；换过则把 seam 的发布点移到
`initialize()`（provider 自报，不再依赖 `sys.modules` 身份）。

## 5. 被否掉的替代

- **B：host 补丁让 `commands.catalog` 收进 exclusive provider 的命令。** 治的是症状所在层，但要维护
  一串 version-gated patch，且和现有 host-patches 机制绑死（该机制已于 2026-10-30 退役）。
- **C：只在文档写"第一轮之后再用"。** 零成本，但用户仍会踩到，且 unknown 的措辞会让人以为是没这个命令。
