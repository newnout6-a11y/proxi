"""Retool direct transport adapter implementing UpstreamDriver."""
from __future__ import annotations

import contextlib

import auth
from credits import CreditsManager
from drivers.base import DriverCapabilities, UpstreamDriver, register_driver


class RetoolDriver(UpstreamDriver):
    driver_id = "retool"
    capabilities = DriverCapabilities(
        driver_id="retool",
        billing_unit="retool_credits",
        uses_retool_tool_schema=True,
        requires_retool_resource=True,
        supports_native_history=True,
        supports_keepwarm=True,
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
        import direct

        async with contextlib.aclosing(
            direct.iter_events(cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens, chunks)
        ) as events:
            async for event in events:
                yield event

    def make_account_manager(self, backend, root_cfg: dict):
        if not hasattr(backend, "refresher"):
            return None
        return CreditsManager(
            backend.cfg.get("base", root_cfg.get("base")),
            (lambda b=backend: auth.upstream_headers(b.cfg)),
            backend.refresher,
        )


register_driver(RetoolDriver())
