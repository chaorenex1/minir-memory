"""adviser — the auxiliary model route the plugin's agents call.

The route (``auxiliary.adviser``) is owned here: ``ctx.register_auxiliary_task("adviser")``
creates the config block, its model-picker entry and the ``AUXILIARY_ADVISER_*`` env bridge, and
``agent_loop`` consumes it for every peer run. This module also registers the read-only host
session search, which now lives in the ``minir`` toolset (``minir_hermes_session_search``).

There is no model-visible consult tool: the reviewer's synchronous entry point was deleted with
its schema, handler, dynamic registration, prompt section and toolset. ``AdviserMode.consult``
remains a programmatic entry for callers inside this process; the similarly named
``worker._adviser_consult`` is the layer passes' internal route and stays.

Everything is lazy: importing this module never touches the host's config, credentials, or model
client, so the memory provider keeps loading when the adviser is never used.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import file_guard, session_search

logger = logging.getLogger(__name__)

TASK_KEY = "adviser"          # must be alnum/underscore; must not collide with a built-in aux task

# Plugin-owned keys layered under the user's ``auxiliary.adviser`` block. ``provider``/``model``
# pass straight through to ``agent_loop``; everything else (credentials, reasoning, budget) is
# resolved by the loop itself.
_CFG_DEFAULTS: dict[str, Any] = {"toolsets": []}


def load_config() -> dict[str, Any]:
    """Effective ``auxiliary.adviser`` config: plugin defaults < user config."""
    try:
        from hermes_cli.config import load_config_readonly

        aux = (load_config_readonly() or {}).get("auxiliary") or {}
        cfg = dict(aux.get(TASK_KEY) or {}) if isinstance(aux, dict) else {}
    except Exception:  # health: allow BLE001 -- a config read must never break the tool
        logger.debug("adviser: auxiliary config read failed", exc_info=True)
        cfg = {}
    return {**_CFG_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}


def _route(cfg: dict[str, Any]) -> dict[str, Any]:
    """Only the knobs the adviser owns; credentials/budget are resolved inside the loop."""
    return {"model": str(cfg.get("model") or ""), "provider": str(cfg.get("provider") or ""),
            "api_mode": str(cfg.get("api_mode") or ""), "toolsets": cfg.get("toolsets")}


def project_dir(cfg: dict[str, Any] | None = None) -> Optional[str]:
    """Read root an adviser run may use outside HERMES_HOME.

    ``None`` means "use the default": every peer run already inherits the cwd's git root.
    ``auxiliary.adviser.project_dir`` overrides it; an unusable path returns ``""`` so the run
    gets no project read instead of silently falling back to another directory.
    """
    cfg = load_config() if cfg is None else cfg
    explicit = str(cfg.get("project_dir") or "").strip()
    if not explicit:
        return None
    root = file_guard.project_root(explicit)
    if root is None:
        logger.warning("adviser: project_dir is not a usable directory: %s", explicit)
        return ""
    return str(root)


def register(ctx, provider: Any = None) -> None:
    """Register the read-only host session search and the auxiliary route."""
    # Read-only, stdlib-only store access. The handler is synchronous on purpose: one bounded
    # SQLite read with no model call, so the host needs no thread bridge. The caller class and
    # platform are injected here: an agent run is scoped to the current host session, a host
    # caller to its own platform's session sources.
    def _session_search_handler(args: dict[str, Any], **kwargs: Any) -> str:
        from . import agent_history

        kwargs.pop("provider", None)
        live = provider
        return session_search.handle(
            args,
            hermes_home=str(getattr(live, "_hermes_home", "") or ""),
            state_db=str(getattr(live, "_state_db", "") or ""),
            platform=str(getattr(live, "_platform", "") or "cli"),
            caller="agent" if agent_history.current_layer() else "host",
            session_id=str(getattr(live, "_session_id", "") or ""),
        )

    ctx.register_tool(
        name=session_search.TOOL_NAME,
        toolset=session_search.TOOLSET,
        schema=session_search.SCHEMA,
        handler=_session_search_handler,
        is_async=False,
        description=("Search/read the host's durable session store (state.db): FTS5 message "
                     "search, session browse, transcript read, message window, stats."),
        emoji="🗂️",
    )
    # The route is separate from the tool: a host that refuses the auxiliary key (another plugin
    # owns it) still gets the read-only search, because the tool above already landed.
    ctx.register_auxiliary_task(
        TASK_KEY,
        display_name="Adviser",
        description="Second-opinion adviser: model route for the memory plugin's in-process agent loop.",
        defaults={"toolsets": []},
    )
