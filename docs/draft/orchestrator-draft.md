# 任务编排器 · 草稿（记忆层之上）

状态：草稿，落在本插件 `docs/` 下。没有落地接线，没有建卡，没有派工，没有改 config 或任何现有代码。

## 0. 上一版错在哪

上一版把编排器当兄弟子系统：自带 `orch/` 包、自带 `decisions.jsonl`、以 Hermes Kanban 为状态唯一来源、编译出 `hermes kanban create` 命令。方向错了：这个插件是记忆层，编排器必须是**构建在记忆层之上的功能**，它不该自建一套状态、日志和调度。

这一版把状态、决策证据、遥测全部落在记忆层已有的表和模块上，只新增纯逻辑模块，不新增数据库、不新增日志文件。

## 1. 修正后的定位

编排器 = 记忆层里的第三个调度器，和现有的两个同构：

    extraction.py     ExtractionScheduler     空闲会话的抽取
    card_review.py    CardReviewScheduler     卡片修订复核
    orchestrator_*     OrchestratorScheduler   任务图推进   <- 本草案

它们共用 `scheduler.py` 的 `Scheduler`（`add_turn` / `add_time` / `tick` / 统一账本）和 `agent_loop.run`（带 model / provider / toolsets / usage 记账的执行原语）。

仓库实测依据（34 个模块 / 13 张表）：

    store.py     MinirStore.save_conclusion(kind, content, *, key, subject, evidence, status, version) -> id
                 MinirStore.get_conclusion(kind, key) -> dict | None；唯一键 (kind, key)，upsert 时 version 自增
    scheduler.py Scheduler(tick=...)、add_turn(name, run)、add_time(name, every, run, due=...)、tick(now)
    agent_loop   run(message, *, context, model, provider, api_mode, toolsets, system_message, max_iterations)
    agent        minir_agents 按 agent 名解析 prompt 文件（.agents/ → agents/ → skills/<s>/agents/ → prompts）
    conclusions  id, kind, key, session_id, subject, content, evidence, version, status, created_at, updated_at
    metrics      tool, count, total_ms, max_ms, errors, updated_at
    turn_usage   session_id, turn_id, model, api_calls, prompt/completion/total/cache tokens, reasoning_tokens
    advisories   severity, target, content, decision, evidence, fingerprint, status, injected_at

## 2. 每样东西落在哪

    图定义      profile-local 文件  <store_root>/orchestrator/<graph>.json   文件权威，可版本控制
    节点状态    conclusions(kind='orchestrator_node', key='<graph>:<node>')
                content 留空（正文不进 SQLite，守住文件优先合同）；subject=stage；
                evidence=JSON{graph,stage,attempt,gate,note}；status=状态机当前态
    边          不进表。图文件里已有 deps，缝合后就是推进规则，不复制第二份真相
    路由决策    evidence 里的 agent/model + 返回值的 source（spec|rule|default）
    重试计数    evidence.attempt；超限即 status='blocked'
    执行        agent_loop.run（生产）或注入的 executor（测试）
    预算与耗时  turn_usage / session_usage（agent_loop 自己记）
    工具遥测    metrics（宿主已有的 tool 计数，不改）
    验收        gate 字段；只有 'pass' 允许下游解锁
    调度        Scheduler.add_time("orchestrator", every=60, run=tick)

刻意不用的：

    jobs 表      实测列就这些：layer, session_id, from_ts, to_ts, turns, status, created_at,
                 attempts, reclaims, claimed_at, finished_at, error, processed_ts。没有任何位置装得下
                 节点 id：塞进 session_id 或 error 都是错的。要用它得先加一列 payload，这一版不付
                 这个改动，因为就绪集本来就是每次 tick 从 conclusions 现算的，重启后不需要补队列。
    base 的 kind  不用 kind='trajectory'/'todo'。那两个是值标记，绑 session_id，会被 minir_search
                 和值标记管线当记忆召回；图的中间状态进去会污染召回。改用 kind='orchestrator_node'：
                 同一张表、同一套 upsert 语义（UNIQUE(kind,key)、version 自增），但不进召回。
    Kanban       降级为可选执行器：想让 dev1/dev2 这类 profile 当工人就再写一个 executor，状态仍以 conclusions 为准
    新日志文件   decisions.jsonl 取消，决策证据进 evidence 列，随行可查、可被 minir_search 检索

## 3. 状态机

    pending ──(依赖全 done)──> running ──gate=pass──> done
                                  │
                                  ├──gate!=pass 且 attempt<max_attempts──> failed ──> 下一 tick 重试
                                  └──gate!=pass 且 attempt>=max_attempts─> blocked（交给人）

谁能改：只有 tick 在改。`Scheduler` 的 time task 单线程跑，所以不需要行锁；以后要多进程推进，必须先把 `start()` 换成 SQL 级原子 UPDATE，这一版不假设并发。

## 4. 代码（copy-ready）

### 4.1 图与校验

```python
# file: plugin/minir/orchestrator_spec.py
"""Declarative task graph: stages, edges, validation, topological order."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

STAGES = ("plan", "fork", "delegate", "work", "merge", "review", "ship")
INLINE_STAGES = ("fork", "merge")


class SpecError(ValueError):
    """The graph cannot be scheduled as written."""


@dataclass(frozen=True)
class Route:
    agent: str
    model: str = ""
    provider: str = ""
    toolsets: tuple[str, ...] = ()
    priority: int = 0


@dataclass
class Node:
    id: str
    stage: str
    title: str
    goal: str = ""
    deps: tuple[str, ...] = ()
    route: Route | None = None
    acceptance: tuple[str, ...] = ()
    resolve: str = "card"
    max_attempts: int = 2


@dataclass
class Graph:
    name: str
    nodes: list[Node]
    session_id: str = ""

    def by_id(self) -> dict[str, Node]:
        return {node.id: node for node in self.nodes}


def _assert_acyclic(by_id: dict[str, Node]) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node_id: str) -> None:
        seen = state.get(node_id)
        if seen == 1:
            raise SpecError("cycle: " + " -> ".join(stack[stack.index(node_id):] + [node_id]))
        if seen == 2:
            return
        state[node_id] = 1
        stack.append(node_id)
        for dep in by_id[node_id].deps:
            visit(dep)
        stack.pop()
        state[node_id] = 2

    for node_id in by_id:
        visit(node_id)


def validate(graph: Graph) -> None:
    ids = [node.id for node in graph.nodes]
    duplicated = {node_id for node_id in ids if ids.count(node_id) > 1}
    if duplicated:
        raise SpecError(f"duplicate node ids: {sorted(duplicated)}")
    by_id = graph.by_id()
    for node in graph.nodes:
        if node.stage not in STAGES:
            raise SpecError(f"{node.id}: unknown stage {node.stage!r}")
        if node.resolve not in ("card", "inline"):
            raise SpecError(f"{node.id}: resolve must be card|inline")
        if node.resolve == "inline" and node.stage not in INLINE_STAGES:
            raise SpecError(f"{node.id}: inline is only allowed for {INLINE_STAGES}")
        if node.resolve == "inline" and not node.deps:
            raise SpecError(f"{node.id}: inline node needs a dependency to splice")
        if node.route is None:
            raise SpecError(f"{node.id}: every node carries a route")
        if node.id in node.deps:
            raise SpecError(f"{node.id}: depends on itself")
        if node.max_attempts < 1:
            raise SpecError(f"{node.id}: max_attempts must be >= 1")
        for dep in node.deps:
            if dep not in by_id:
                raise SpecError(f"{node.id}: unknown dependency {dep!r}")
    _assert_acyclic(by_id)
    plans = [node for node in graph.nodes if node.stage == "plan"]
    ships = [node for node in graph.nodes if node.stage == "ship"]
    if not plans:
        raise SpecError("graph needs at least one plan node")
    if not ships:
        raise SpecError("graph needs at least one ship node")
    children: dict[str, set[str]] = {node.id: set() for node in graph.nodes}
    for node in graph.nodes:
        for dep in node.deps:
            children[dep].add(node.id)
    for ship in ships:
        if children[ship.id]:
            raise SpecError(f"{ship.id}: ship node must be terminal")
    reachable = {plan.id for plan in plans}
    frontier = list(reachable)
    while frontier:
        current = frontier.pop()
        for child in children[current]:
            if child not in reachable:
                reachable.add(child)
                frontier.append(child)
    stranded = sorted(node.id for node in graph.nodes if node.id not in reachable)
    if stranded:
        raise SpecError(f"not downstream of any plan node: {stranded}")


def splice(graph: Graph) -> dict[str, frozenset[str]]:
    """Map every schedulable node to its non-inline prerequisites."""
    by_id = graph.by_id()

    def resolve(node_id: str) -> frozenset[str]:
        found: set[str] = set()
        for dep in by_id[node_id].deps:
            if by_id[dep].resolve == "inline":
                found |= resolve(dep)
            else:
                found.add(dep)
        return frozenset(found)

    return {node.id: resolve(node.id) for node in graph.nodes if node.resolve != "inline"}


def topo_order(graph: Graph) -> list[str]:
    parents = splice(graph)
    sequence = [node.id for node in graph.nodes if node.id in parents]
    children: dict[str, list[str]] = {node_id: [] for node_id in sequence}
    pending = {node_id: len(parents[node_id]) for node_id in sequence}
    for node_id in sequence:
        for parent in parents[node_id]:
            children[parent].append(node_id)
    ready = [node_id for node_id in sequence if pending[node_id] == 0]
    order: list[str] = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for child in children[node_id]:
            pending[child] -= 1
            if pending[child] == 0:
                ready.append(child)
        ready.sort(key=sequence.index)
    if len(order) != len(sequence):
        raise SpecError("cycle survived validation")
    return order


def load(path: str | Path) -> Graph:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes = []
    for item in raw["nodes"]:
        data = dict(item)
        nodes.append(Node(
            deps=tuple(data.pop("deps", ())),
            acceptance=tuple(data.pop("acceptance", ())),
            route=Route(**data.pop("route")),
            **data,
        ))
    graph = Graph(name=raw["name"], nodes=nodes, session_id=raw.get("session_id", ""))
    validate(graph)
    return graph
```

### 4.2 路由

```python
# file: plugin/minir/orchestrator_router.py
"""Pick the agent and model for a node. Deterministic: no invented confidence."""

from __future__ import annotations

from dataclasses import dataclass

from .orchestrator_spec import Node, Route, SpecError


@dataclass(frozen=True)
class Rule:
    stage: str
    needle: str
    agent: str
    model: str = ""
    provider: str = ""
    toolsets: tuple[str, ...] = ()

    def matches(self, node: Node) -> bool:
        if node.stage != self.stage:
            return False
        return self.needle.lower() in f"{node.title}\n{node.goal}".lower()


@dataclass(frozen=True)
class Decision:
    node_id: str
    stage: str
    route: Route
    source: str
    sharp: bool


class Router:
    """Order: node route -> rule -> stage default."""

    def __init__(self, defaults: dict[str, Route], rules: tuple[Rule, ...] = ()):
        self.defaults = dict(defaults)
        self.rules = tuple(rules)

    def decide(self, node: Node) -> Decision:
        if node.route is not None:
            return Decision(node.id, node.stage, node.route, "spec", node.resolve == "inline")
        for rule in self.rules:
            if rule.matches(node):
                route = Route(agent=rule.agent, model=rule.model, provider=rule.provider,
                              toolsets=rule.toolsets)
                return Decision(node.id, node.stage, route, "rule", node.resolve == "inline")
        fallback = self.defaults.get(node.stage)
        if fallback is None:
            raise SpecError(f"{node.stage}: no route, no matching rule, no stage default")
        return Decision(node.id, node.stage, fallback, "default", node.resolve == "inline")
```

### 4.3 状态（落在 conclusions）

```python
# file: plugin/minir/orchestrator_state.py
"""Node lifecycle on top of the memory layer: one conclusion row per node."""

from __future__ import annotations

import json

from .orchestrator_spec import Graph, Node, splice, topo_order

KIND = "orchestrator_node"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"


def key(graph_name: str, node_id: str) -> str:
    return f"{graph_name}:{node_id}"


def read(store, graph_name: str, node_id: str) -> dict:
    row = store.get_conclusion(KIND, key(graph_name, node_id))
    if row is None:
        return {"status": "", "attempt": 0, "gate": "", "note": ""}
    raw = row.get("evidence") or "[]"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "status": str(row.get("status") or ""),
        "attempt": int(data.get("attempt", 0) or 0),
        "gate": str(data.get("gate", "") or ""),
        "note": str(data.get("note", "") or ""),
    }


def write(store, graph_name: str, node_id: str, *, stage: str, status: str,
          attempt: int, gate: str = "", note: str = "") -> int:
    """Body stays out of SQLite: content is empty, the graph file owns the text."""
    evidence = json.dumps({"graph": graph_name, "stage": stage, "attempt": int(attempt),
                           "gate": gate, "note": note[:500]}, ensure_ascii=False)
    return store.save_conclusion(KIND, "", key=key(graph_name, node_id), subject=stage,
                                 evidence=evidence, status=status)


def start(store, graph_name: str, node: Node) -> int:
    attempt = read(store, graph_name, node.id)["attempt"] + 1
    write(store, graph_name, node.id, stage=node.stage, status=RUNNING, attempt=attempt)
    return attempt


def finish(store, graph_name: str, node: Node, *, gate: str, attempt: int, note: str = "") -> str:
    """gate != 'pass' never yields 'done', so a failed gate cannot unlock children."""
    if gate == "pass":
        status = DONE
    elif attempt >= node.max_attempts:
        status = BLOCKED
    else:
        status = FAILED
    write(store, graph_name, node.id, stage=node.stage, status=status, attempt=attempt,
          gate=gate, note=note)
    return status


def ready_nodes(graph: Graph, store) -> list[str]:
    """Schedulable nodes whose spliced prerequisites are all done, in graph order."""
    parents = splice(graph)
    done = {node_id for node_id in parents if read(store, graph.name, node_id)["status"] == DONE}
    ready: list[str] = []
    for node_id in topo_order(graph):
        status = read(store, graph.name, node_id)["status"]
        if status in (RUNNING, DONE, BLOCKED):
            continue
        if parents[node_id] <= done:
            ready.append(node_id)
    return ready
```

### 4.4 调度器

```python
# file: plugin/minir/orchestrator_scheduler.py
"""One tick advances every node whose dependencies are done."""

from __future__ import annotations

from . import orchestrator_state as state
from .orchestrator_router import Router
from .orchestrator_spec import Graph


class OrchestratorScheduler:
    """executor(node, decision) -> {"gate": "pass" | "fail", "note": str}."""

    def __init__(self, graph: Graph, store, router: Router, executor):
        self.graph = graph
        self.store = store
        self.router = router
        self.executor = executor

    def tick(self) -> list[dict]:
        node_by_id = self.graph.by_id()
        advanced: list[dict] = []
        for node_id in state.ready_nodes(self.graph, self.store):
            node = node_by_id[node_id]
            decision = self.router.decide(node)
            attempt = state.start(self.store, self.graph.name, node)
            try:
                outcome = self.executor(node, decision) or {}
            except Exception as exc:  # one node crashing must not stop the tick
                status = state.finish(self.store, self.graph.name, node, gate="fail",
                                      attempt=attempt, note=f"executor raised {exc!r}")
                advanced.append({"node": node_id, "status": status,
                                 "agent": decision.route.agent, "source": decision.source})
                continue
            gate = str(outcome.get("gate", "fail") or "fail").lower()
            status = state.finish(self.store, self.graph.name, node, gate=gate,
                                  attempt=attempt, note=str(outcome.get("note", "")))
            advanced.append({"node": node_id, "status": status,
                             "agent": decision.route.agent, "source": decision.source})
        return advanced

    def register(self, scheduler, *, every: float = 60.0, name: str = "orchestrator") -> None:
        """Attach to the memory layer's unified scheduler."""
        scheduler.add_time(name, every, self.tick)
```

### 4.5 测试

```python
# file: tests/test_orchestrator.py
"""The orchestrator runs on the memory layer: conclusions hold node state."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def orch(plugin_package):
    names = ("orchestrator_spec", "orchestrator_router", "orchestrator_state",
             "orchestrator_scheduler")
    return {name: importlib.import_module(f"{plugin_package.__name__}.{name}") for name in names}


@pytest.fixture()
def store(plugin_package, tmp_path):
    instance = plugin_package.store.MinirStore(tmp_path / "mem" / "memory.db")
    yield instance
    instance.close()


def build(orch):
    spec = orch["orchestrator_spec"]
    route = spec.Route(agent="default")
    nodes = [
        spec.Node(id="plan", stage="plan", title="Plan", goal="scope it", route=route),
        spec.Node(id="fork", stage="fork", title="Pick files", deps=("plan",), route=route,
                  resolve="inline"),
        spec.Node(id="work", stage="work", title="Do it", deps=("fork",), route=route,
                  acceptance=("pytest exits 0",), max_attempts=2),
        spec.Node(id="review", stage="review", title="Review", deps=("work",), route=route),
        spec.Node(id="ship", stage="ship", title="Ship", deps=("review",), route=route),
    ]
    return spec.Graph(name="cycle", nodes=nodes)


def test_rejects_cycle(orch):
    spec = orch["orchestrator_spec"]
    graph = build(orch)
    graph.by_id()["plan"].deps = ("ship",)
    with pytest.raises(spec.SpecError, match="cycle"):
        spec.validate(graph)


def test_rejects_unknown_dependency(orch):
    spec = orch["orchestrator_spec"]
    graph = build(orch)
    graph.by_id()["work"].deps = ("ghost",)
    with pytest.raises(spec.SpecError, match="unknown dependency"):
        spec.validate(graph)


def test_inline_is_limited_to_fork_and_merge(orch):
    spec = orch["orchestrator_spec"]
    graph = build(orch)
    graph.by_id()["work"].resolve = "inline"
    with pytest.raises(spec.SpecError, match="inline is only allowed"):
        spec.validate(graph)


def test_splice_drops_the_inline_node(orch):
    spec = orch["orchestrator_spec"]
    parents = spec.splice(build(orch))
    assert "fork" not in parents
    assert parents["work"] == frozenset({"plan"})
    assert parents["review"] == frozenset({"work"})


def test_state_row_keeps_the_body_out_of_sqlite(orch, store):
    state = orch["orchestrator_state"]
    graph = build(orch)
    state.start(store, graph.name, graph.by_id()["plan"])
    row = store.get_conclusion(state.KIND, "cycle:plan")
    assert row is not None
    assert row["status"] == "running"
    assert row["content"] == ""
    assert row["subject"] == "plan"


def test_only_the_plan_is_ready_on_an_empty_store(orch, store):
    state = orch["orchestrator_state"]
    assert state.ready_nodes(build(orch), store) == ["plan"]


def test_a_done_parent_unlocks_the_child(orch, store):
    state = orch["orchestrator_state"]
    graph = build(orch)
    node = graph.by_id()["plan"]
    attempt = state.start(store, graph.name, node)
    assert state.finish(store, graph.name, node, gate="pass", attempt=attempt) == state.DONE
    assert state.ready_nodes(graph, store) == ["work"]


def test_failed_gate_does_not_unlock_the_child(orch, store):
    state = orch["orchestrator_state"]
    graph = build(orch)
    node = graph.by_id()["plan"]
    attempt = state.start(store, graph.name, node)
    assert state.finish(store, graph.name, node, gate="fail", attempt=attempt) == state.FAILED
    assert state.ready_nodes(graph, store) == ["plan"]


def test_retries_end_in_blocked(orch, store):
    state = orch["orchestrator_state"]
    graph = build(orch)
    node = graph.by_id()["plan"]
    first = state.start(store, graph.name, node)
    assert state.finish(store, graph.name, node, gate="fail", attempt=first) == state.FAILED
    second = state.start(store, graph.name, node)
    assert second == 2
    assert state.finish(store, graph.name, node, gate="fail", attempt=second) == state.BLOCKED
    assert state.ready_nodes(graph, store) == []


def test_tick_advances_plan_then_work(orch, store):
    router_mod = orch["orchestrator_router"]
    module = orch["orchestrator_scheduler"]
    graph = build(orch)
    calls: list[str] = []

    def executor(node, decision):
        calls.append(node.id)
        return {"gate": "pass", "note": f"{decision.route.agent} did it"}

    runner = module.OrchestratorScheduler(graph, store, router_mod.Router(defaults={}), executor)
    assert [row["node"] for row in runner.tick()] == ["plan"]
    assert [row["node"] for row in runner.tick()] == ["work"]
    assert [row["node"] for row in runner.tick()] == ["review"]
    assert [row["node"] for row in runner.tick()] == ["ship"]
    assert runner.tick() == []
    assert calls == ["plan", "work", "review", "ship"]


def test_executor_crash_counts_as_a_failed_gate(orch, store):
    router_mod = orch["orchestrator_router"]
    module = orch["orchestrator_scheduler"]
    graph = build(orch)

    def executor(node, decision):
        raise RuntimeError("boom")

    runner = module.OrchestratorScheduler(graph, store, router_mod.Router(defaults={}), executor)
    rows = runner.tick()
    assert rows[0]["status"] == "failed"
    assert runner.tick()[0]["status"] == "blocked"


def test_router_rule_wins_over_the_stage_default(orch):
    router_mod = orch["orchestrator_router"]
    spec = orch["orchestrator_spec"]
    node = spec.Node(id="w", stage="work", title="Retry the flaky test")
    rules = (router_mod.Rule(stage="work", needle="retry", agent="explorer"),)
    decision = router_mod.Router(defaults={"work": spec.Route(agent="worker")}, rules=rules).decide(node)
    assert decision.route.agent == "explorer"
    assert decision.source == "rule"


def test_router_prefers_the_node_route(orch):
    router_mod = orch["orchestrator_router"]
    spec = orch["orchestrator_spec"]
    node = spec.Node(id="w", stage="work", title="x",
                     route=spec.Route(agent="researcher", model="m1"))
    decision = router_mod.Router(defaults={"work": spec.Route(agent="worker")}).decide(node)
    assert (decision.route.agent, decision.route.model, decision.source) == ("researcher", "m1", "spec")


def test_register_attaches_a_time_task(orch, store):
    router_mod = orch["orchestrator_router"]
    module = orch["orchestrator_scheduler"]
    seen: dict = {}

    class FakeScheduler:
        def add_time(self, name, every, run, **kwargs):
            seen.update(name=name, every=every, run=run)

    runner = module.OrchestratorScheduler(build(orch), store, router_mod.Router(defaults={}),
                                          lambda node, decision: {})
    runner.register(FakeScheduler(), every=30)
    assert (seen["name"], seen["every"]) == ("orchestrator", 30)
    assert callable(seen["run"])


def test_example_graph_loads_and_splices(orch):
    spec = orch["orchestrator_spec"]
    path = Path(__file__).resolve().parents[1] / "examples" / "cycle.json"
    graph = spec.load(path)
    parents = spec.splice(graph)
    assert len(graph.nodes) == 9
    assert sorted(parents) == ["delegate", "plan", "review", "ship",
                               "work-api", "work-docs", "work-ui"]
    assert parents["review"] == frozenset({"work-api", "work-ui", "work-docs"})
    order = spec.topo_order(graph)
    assert order.index("plan") < order.index("delegate") < order.index("work-api")
    assert order.index("work-api") < order.index("review") < order.index("ship")
```

### 4.6 示例图

```json
# file: examples/cycle.json
{
  "name": "cycle",
  "session_id": "host-session",
  "nodes": [
    {"id": "plan", "stage": "plan", "title": "Plan: scope the change",
     "goal": "Read the repo, write the acceptance criteria, list the files in play.",
     "route": {"agent": "default"}},
    {"id": "fork", "stage": "fork", "title": "Pick the files per tool",
     "deps": ["plan"], "resolve": "inline", "route": {"agent": "default"}},
    {"id": "delegate", "stage": "delegate", "title": "Split into three independent items",
     "deps": ["plan"], "route": {"agent": "default"}},
    {"id": "work-api", "stage": "work", "title": "Work: API surface",
     "goal": "Add the endpoint and its handler.",
     "deps": ["delegate", "fork"],
     "acceptance": ["pytest -q exits 0", "no public signature change"],
     "route": {"agent": "worker", "model": "deepseek-v4-flash", "provider": "deepseek",
               "toolsets": ["shell", "files"]},
     "max_attempts": 2},
    {"id": "work-ui", "stage": "work", "title": "Work: UI surface",
     "goal": "Wire the form to the new endpoint.",
     "deps": ["delegate", "fork"],
     "acceptance": ["storybook builds"],
     "route": {"agent": "worker"}, "max_attempts": 2},
    {"id": "work-docs", "stage": "work", "title": "Work: docs",
     "goal": "Document the endpoint and the migration note.",
     "deps": ["delegate", "fork"],
     "acceptance": ["README section exists"],
     "route": {"agent": "researcher"}},
    {"id": "merge", "stage": "merge", "title": "Merge the three branches",
     "deps": ["work-api", "work-ui", "work-docs"], "resolve": "inline",
     "route": {"agent": "default"}},
    {"id": "review", "stage": "review", "title": "Review the merged diff",
     "deps": ["merge"],
     "acceptance": ["gate=pass recorded", "no unrelated files touched"],
     "route": {"agent": "reviewer"}},
    {"id": "ship", "stage": "ship", "title": "Ship: hand off the result",
     "deps": ["review"], "route": {"agent": "default"}}
  ]
}
```

## 5. 跑起来是什么样

    graph  = orchestrator_spec.load("<store_root>/orchestrator/cycle.json")
    router = orchestrator_router.Router(defaults={"work": Route(agent="worker")})
    runner = orchestrator_scheduler.OrchestratorScheduler(graph, store, router, executor)
    runner.register(scheduler, every=60)      # 挂到记忆层已有的 Scheduler 上

一次 tick 只推进"依赖已 done"的节点；`fork` 与 `merge` 不出现在任何一张表里（inline 在编译期就消解）。

换成真实执行。`agent_loop.run` 返回 `AgentRun`（`answer` / `api_calls` / `model` / `provider` / `usage`），**没有 `ok` 字段**；`run_with_timeout` 超时返回 `None`，不是抛异常。两个都得显式处理，否则超时会被当成"跑完了但没结果"：

    def make_executor(agent_loop, accept=None, timeout=900.0):
        """accept(node, run) -> bool 才是真验收；不给就只有'有答案且没超时'这条底线。"""

        def executor(node, decision):
            result = agent_loop.run_with_timeout(
                lambda: agent_loop.run(message=node.goal or node.title,
                                       model=decision.route.model,
                                       provider=decision.route.provider,
                                       toolsets=list(decision.route.toolsets) or None),
                timeout)
            if result is None:
                return {"gate": "fail", "note": f"timeout after {timeout}s"}
            answer = (result.answer or "").strip()
            if not answer:
                return {"gate": "fail", "note": f"empty answer, calls={result.api_calls}"}
            passed = accept is None or bool(accept(node, result))
            tokens = result.usage.get("total_tokens", "?")
            return {"gate": "pass" if passed else "fail",
                    "note": f"{result.model}/{result.provider} calls={result.api_calls} tokens={tokens}"}

        return executor

## 6. 与截图面板的映射

    截图面板              记忆层对应物                              状态
    ─────────────────────────────────────────────────────────────────────────
    主会话 / 子代理卡      conclusions(kind='orchestrator_node')     本草案
    JEV fork 层            inline 节点 + evidence 里的 source        结构就位，无分类器
    卡片状态行             status: pending/running/done/failed       本草案
    顾问左栏               advisories(severity,target,decision)      现成数据，未接
    which tool 条形        metrics(tool,count,total_ms,errors)       现成数据，未接
    每节点 token           turn_usage(session_id,turn_id,model,…)    现成数据，未接
    时间轴 / who sees what  未实现                                    未实现
    状态栏                session_usage 汇总                        现成数据，未接

比上一版更贴：截图里的成本和耗时数字，记忆层本来就有账（turn_usage / metrics），不需要另建遥测。

## 7. 五个坑

1. `gate=fail` 绝不能写 `done`。原生 Kanban 只在 status 上判断，会放过下游；这里把规则写进 `finish()`，有单测锁住。
2. `save_conclusion` 每次 upsert 都把 `version` 加一，它天然是状态转移计数器；但 `created_at` 只在首次写入，别拿它当"最后一次推进时间"。
3. `content` 必须留空。正文进 SQLite 会撞记忆层的文件优先合同（正文在 profile-local 文件，SQLite 只存生命周期与投影）。节点正文来自图文件。
4. tick 是单线程的，不要在多进程里同时推进同一个图；要多进程得先把 `start()` 改成 SQL 级原子 UPDATE。
5. 执行原语有两个反直觉点：`agent_loop.run` 返回 `AgentRun` 对象，没有 `ok` 字段（用 `answer` / `api_calls` / `usage`）；`run_with_timeout` 超时返回 `None` 而不是抛异常。executor 里不显式判断 `None`，超时会被记成"跑完了但没答案"，重试计数也照走。

## 8. 本轮验证

方法：把本文档里每个 `# file:` 代码块抽到 `hermes/cache/scratch/orch-v2/`（连同 `plugin/minir` 的副本和仓库的 `tests/conftest.py`），在该目录跑 pytest。用的是真实 `MinirStore` 写在临时 SQLite 上，不碰宿主数据库、不调模型。

逐块：

    plugin/minir/orchestrator_spec.py       173 行   导入通过；校验、缝合、拓扑、示例图被 5 个测试覆盖
    plugin/minir/orchestrator_router.py      52 行   导入通过；spec > rule > default 被 2 个测试覆盖
    plugin/minir/orchestrator_state.py       78 行   导入通过；状态转移与就绪集被 5 个测试覆盖
    plugin/minir/orchestrator_scheduler.py   43 行   导入通过；tick 与 register 被 3 个测试覆盖
    tests/test_orchestrator.py              197 行   15 passed in 1.53s
    examples/cycle.json                      39 行   load + splice + topo：9 节点、7 张可调度、review 挂 3 张 work

验证抓到并修掉了一个真实缺陷（不是笔误）：

`test_tick_advances_plan_then_work` 初版断言第三次 tick 返回空，实际它推进了 `review`。也就是 tick 链是 plan → work → review → ship 一路到底，不是我以为的两步。测试改成断言整条链，这条链现在被锁住了。

同时确认了两件合同事：节点行的 `content` 确实是空串（正文没进 SQLite），`subject` 等于 stage；`gate=fail` 之后 `ready_nodes` 不会放出下游。

## 9. 未验证 / 下一刀

未验证：
- 没接真实 `agent_loop.run` executor（测试用假 executor）：模型调用、usage 记账、toolsets 传入、超时分支都没实测。§5 的 `make_executor` 是按已核对的签名写对的，但从没跑过。
- 验收是空的：`accept(node, run)` 没有实现，默认只有"有答案且没超时"这条底线。真正的 gate 得按项目自己的验收标准写。
- 没接真 `Scheduler`，`register()` 只验证到 `add_time` 被调用；真实 tick 与宿主轮次的关系没测。
- `examples/cycle.json` 验证到 load + splice + topo，没在真实 store 上把 7 个节点跑完一轮 tick。
- 并发假设：单线程 tick，没压测，也没有多进程保护。

下一刀，按顺序：
1. `executor` 接 `agent_loop.run`，跑一张真实 3 节点图（plan → work → review），看 usage 是否落进 `turn_usage`。
2. `register()` 接真 `Scheduler`，确认 60s tick 不与记忆抽取抢同一轮。
3. 投影：从 conclusions + metrics + turn_usage 渲染截图那套面板。
