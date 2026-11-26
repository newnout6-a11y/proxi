"""Universal model catalog: models.dev enrichment, /models auto-discovery, and route aliases/fallbacks."""
from catalog.models_dev import calculate_cost_usd, lookup_metadata
from catalog.discovery import UniversalCatalogRefresher, discover_provider_models
from catalog.router import build_provider_catalog, build_route_aliases, select_candidate_backend

__all__ = [
    "UniversalCatalogRefresher",
    "build_provider_catalog",
    "build_route_aliases",
    "calculate_cost_usd",
    "discover_provider_models",
    "lookup_metadata",
    "select_candidate_backend",
]
