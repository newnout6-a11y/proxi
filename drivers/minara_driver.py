"""Minara Bearer upstream adapter implementing UpstreamDriver."""
from __future__ import annotations

import contextlib

from accounts import MinaraAccount
from drivers.base import DriverCapabilities, UpstreamDriver, register_driver


class MinaraDriver(UpstreamDriver):
    driver_id = "minara"
    capabilities = DriverCapabilities(
        driver_id="minara",
        billing_unit="minara_credits",
        uses_retool_tool_schema=False,
        requires_retool_resource=False,
        supports_native_history=True,
        supports_keepwarm=False,
        supports_cache_control=True,
        supports_prompt_cache_key=True,
    )

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
        import minara

        async with contextlib.aclosing(
            minara.iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks)
        ) as events:
            async for event in events:
                yield event

    def make_account_manager(self, backend, root_cfg: dict):
        return MinaraAccount(backend.cfg, backend=backend)


register_driver(MinaraDriver())
