"""Adapter registry — every channel Chronicle can ingest.

Importing this module registers everything. `ADAPTERS` maps source name to
class; `chronicle.sources` holds the deployment policy for each one.
"""

from .base import (ADAPTERS, Adapter, ApiAdapter, Density, FileAdapter,
                   SourceEvent, SqlAdapter, ingest_all, register)

# Local databases and files
from . import dawarich, firefly, forgejo, immich, karakeep  # noqa: F401
from . import miniflux, nytka, owntracks, paperless, telegram, wakapi  # noqa: F401

# HTTP / MCP
from . import api_sources  # noqa: F401

__all__ = ["ADAPTERS", "Adapter", "ApiAdapter", "Density", "FileAdapter",
           "SourceEvent", "SqlAdapter", "ingest_all", "register"]
