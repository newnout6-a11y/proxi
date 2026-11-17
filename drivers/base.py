"""Polymorphic UpstreamDriver contract and registry.

Every upstream transport — universal (`openai-chat`, `openai-responses`, `anthropic-messages`) as well as
the specialized `retool` and `minara` drivers — implements `UpstreamDriver`. Core modules (`session.py`,
`orchestrator.py`, `proxy.py`, `admin.py`) dispatch through `get_driver(cfg)` rather than hardcoding
provider-specific branches.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class DriverCapabilities:
    """Declarative protocol & cache properties of an upstream driver."""
    driver_id: str
    billing_unit: str = "usd"  # "usd" | "retool_credits" | "minara_credits"
    uses_retool_tool_schema: bool = False
    requires_retool_resource: bool = False
    supports_native_history: bool = True
    supports_keepwarm: bool = False
    supports_cache_control: bool = False
    supports_prompt_cache_key: bool = False


class UpstreamDriver(ABC):
    """Unified interface implemented by every upstream protocol driver."""

    driver_id: str
    capabilities: DriverCapabilities

    @abstractmethod
    async def stream_turn(
        self,
        cfg: dict,
        session,
        model_provider: dict,
        prompt: str,
        head: str | None,
        tools: list | None,
        tool_choice,
        max_tokens: int | None,
        chunks: list | None = None,
    ):
        """Async generator yielding normalized relay events for one upstream attempt."""
        raise NotImplementedError

    def make_account_manager(self, backend, root_cfg: dict):
        """Return the account balance/usage manager for `backend` (`CreditsManager`, `MinaraAccount`, `ProviderBalanceAccount`)."""
        from drivers.balance_checkers import ProviderBalanceAccount
        return ProviderBalanceAccount(backend.cfg, backend=backend)

    async def discover_models(self, cfg: dict) -> list[dict]:
        """Fetch raw model descriptors offered by this upstream endpoint; [] if unsupported."""
        return []


DRIVERS: dict[str, UpstreamDriver] = {}


def register_driver(driver: UpstreamDriver) -> UpstreamDriver:
    DRIVERS[driver.driver_id] = driver
    return driver


def driver_name_of(cfg_or_entry: dict | None) -> str:
    """Resolve canonical driver_id from an account config or catalog entry."""
    if not isinstance(cfg_or_entry, dict):
        return "retool"
    explicit = cfg_or_entry.get("driver") or cfg_or_entry.get("upstream")
    if explicit in (None, "", "retool"):
        return "retool"
    if explicit == "minara":
        return "minara"
    if explicit in ("openai", "openai-chat"):
        return "openai-chat"
    if explicit in ("responses", "openai-responses"):
        return "openai-responses"
    if explicit in ("anthropic", "anthropic-messages"):
        return "anthropic-messages"
    return str(explicit)


def get_driver(cfg_or_name: dict | str | None) -> UpstreamDriver:
    """Look up the registered `UpstreamDriver` for a driver name or account/model config dict."""
    _ensure_builtin_drivers()
    name = cfg_or_name if isinstance(cfg_or_name, str) else driver_name_of(cfg_or_name)
    driver = DRIVERS.get(name)
    if driver is None:
        import apierr
        raise apierr.service_unavailable(f"Upstream driver '{name}' is not registered")
    return driver


_INITIALIZED = False


def _ensure_builtin_drivers():
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True
    from drivers import minara_driver, retool_driver  # noqa: F401
    for mod_name in ("openai_chat", "openai_responses", "anthropic_messages"):
        try:
            __import__(f"drivers.{mod_name}")
        except ImportError:
            pass
