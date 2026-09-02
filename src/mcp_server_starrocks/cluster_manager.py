# Copyright 2021-present StarRocks, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Named StarRocks cluster configuration and connection routing.

The MCP tool layer should never construct database connections directly.  It
resolves a stable cluster id here and receives a DBClient dedicated to that
cluster.  Clients are created lazily so one unavailable cluster does not prevent
the MCP server from starting or make unrelated clusters unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Optional

from .db_client import DBClient
from .db_summary_manager import DatabaseSummaryManager


_CLUSTER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_REFERENCE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ClusterConfigurationError(ValueError):
    """Raised when the configured cluster catalog is invalid."""


class ClusterSelectionError(ValueError):
    """Raised when a request cannot resolve an unambiguous target cluster."""


@dataclass(frozen=True)
class ClusterProfile:
    """A named, server-side StarRocks connection profile."""

    cluster_id: str
    settings: Mapping[str, Any] | None
    description: str | None = None

    @property
    def is_legacy(self) -> bool:
        """Whether this profile is synthesized from the legacy STARROCKS_* env."""
        return self.settings is None

    def public_dict(self, default_cluster_id: str | None) -> dict[str, Any]:
        """Return non-secret metadata safe to expose through MCP."""
        settings = self.settings or {}
        default_database = settings.get("database", settings.get("db"))
        return {
            "cluster_id": self.cluster_id,
            "description": self.description,
            "default_database": default_database,
            "protocol": (
                "arrow-flight-sql"
                if settings.get("fe_arrow_flight_sql_port")
                or settings.get("arrow_flight_sql_port")
                else "mysql"
            ),
            "is_default": self.cluster_id == default_cluster_id,
        }


def _expand_environment_references(value: Any) -> Any:
    """Expand ${NAME} references recursively and fail on missing variables."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ClusterConfigurationError(
                    f"Cluster configuration references unset environment variable '{name}'."
                )
            return os.environ[name]

        return _ENV_REFERENCE_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_environment_references(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _expand_environment_references(item)
            for key, item in value.items()
        }
    return value


def _load_cluster_document() -> dict[str, Any] | None:
    """Load the optional multi-cluster JSON document from env or a file."""
    config_file = os.getenv("STARROCKS_CLUSTERS_FILE")
    config_json = os.getenv("STARROCKS_CLUSTERS")
    if config_file and config_json:
        raise ClusterConfigurationError(
            "Set only one of STARROCKS_CLUSTERS_FILE or STARROCKS_CLUSTERS."
        )
    if config_file:
        path = Path(os.path.expanduser(config_file))
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ClusterConfigurationError(
                f"Unable to read STARROCKS_CLUSTERS_FILE '{path}': {exc}"
            ) from exc
    elif config_json:
        raw = config_json
    else:
        return None

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClusterConfigurationError(
            f"Invalid StarRocks clusters JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ClusterConfigurationError("StarRocks clusters configuration must be a JSON object.")
    return _expand_environment_references(document)


def load_cluster_profiles() -> tuple[dict[str, ClusterProfile], str | None]:
    """Load named profiles, preserving legacy single-cluster behavior.

    Multi-cluster format::

        {
          "default_cluster": "prod",
          "clusters": {
            "prod": {"url": "root:${PROD_PASSWORD}@prod-fe:9030/analytics"},
            "staging": {"host": "staging-fe", "user": "root"}
          }
        }

    If neither multi-cluster setting is present, a ``default`` profile delegates
    to the existing STARROCKS_* environment parsing in :class:`DBClient`.
    """
    document = _load_cluster_document()
    if document is None:
        return {"default": ClusterProfile("default", None, "Legacy STARROCKS_* configuration")}, "default"

    raw_clusters = document.get("clusters")
    if not isinstance(raw_clusters, dict) or not raw_clusters:
        raise ClusterConfigurationError(
            "StarRocks clusters configuration requires a non-empty 'clusters' object."
        )

    profiles: dict[str, ClusterProfile] = {}
    for cluster_id, raw_profile in raw_clusters.items():
        if not isinstance(cluster_id, str) or not _CLUSTER_ID_PATTERN.fullmatch(cluster_id):
            raise ClusterConfigurationError(
                "Cluster ids must be 1-64 characters using letters, digits, '.', '_' or '-', "
                "and must start with a letter or digit."
            )
        if not isinstance(raw_profile, dict):
            raise ClusterConfigurationError(
                f"Configuration for cluster '{cluster_id}' must be a JSON object."
            )
        settings = dict(raw_profile)
        description = settings.pop("description", None)
        if description is not None and not isinstance(description, str):
            raise ClusterConfigurationError(
                f"Description for cluster '{cluster_id}' must be a string."
            )
        profiles[cluster_id] = ClusterProfile(cluster_id, settings, description)

    default_cluster_id = document.get("default_cluster")
    if default_cluster_id is not None:
        if not isinstance(default_cluster_id, str) or default_cluster_id not in profiles:
            raise ClusterConfigurationError(
                "'default_cluster' must name one of the configured clusters."
            )
    elif len(profiles) == 1:
        default_cluster_id = next(iter(profiles))

    return profiles, default_cluster_id


class ClusterManager:
    """Own one lazily-created DBClient and summary cache per cluster."""

    def __init__(
        self,
        profiles: Mapping[str, ClusterProfile] | None = None,
        default_cluster_id: str | None = None,
    ):
        if profiles is None:
            loaded_profiles, loaded_default = load_cluster_profiles()
            profiles = loaded_profiles
            if default_cluster_id is None:
                default_cluster_id = loaded_default
        self._profiles = dict(profiles)
        if not self._profiles:
            raise ClusterConfigurationError("At least one StarRocks cluster is required.")
        if default_cluster_id is not None and default_cluster_id not in self._profiles:
            raise ClusterConfigurationError(
                f"Unknown default cluster '{default_cluster_id}'."
            )
        self.default_cluster_id = default_cluster_id
        self._clients: dict[str, DBClient] = {}
        self._summary_managers: dict[str, DatabaseSummaryManager] = {}
        self._lock = threading.RLock()

    @property
    def cluster_ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    @property
    def initialized_cluster_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._clients)

    def list_clusters(self) -> list[dict[str, Any]]:
        return [
            self._profiles[cluster_id].public_dict(self.default_cluster_id)
            for cluster_id in self.cluster_ids
        ]

    def validate_cluster_id(self, cluster_id: str) -> str:
        if cluster_id not in self._profiles:
            available = ", ".join(self.cluster_ids)
            raise ClusterSelectionError(
                f"Unknown StarRocks cluster '{cluster_id}'. Available clusters: {available}."
            )
        return cluster_id

    def resolve_cluster_id(self, cluster_id: str | None = None) -> str:
        if cluster_id:
            return self.validate_cluster_id(cluster_id)
        if self.default_cluster_id:
            return self.default_cluster_id
        if len(self._profiles) == 1:
            return next(iter(self._profiles))
        available = ", ".join(self.cluster_ids)
        raise ClusterSelectionError(
            "Multiple StarRocks clusters are configured and no cluster is selected. "
            f"Available clusters: {available}."
        )

    def get_client(self, cluster_id: str) -> DBClient:
        cluster_id = self.validate_cluster_id(cluster_id)
        with self._lock:
            client = self._clients.get(cluster_id)
            if client is None:
                profile = self._profiles[cluster_id]
                client = DBClient(config=profile.settings, cluster_id=cluster_id)
                self._clients[cluster_id] = client
            return client

    def get_summary_manager(self, cluster_id: str) -> DatabaseSummaryManager:
        cluster_id = self.validate_cluster_id(cluster_id)
        with self._lock:
            manager = self._summary_managers.get(cluster_id)
            if manager is None:
                manager = DatabaseSummaryManager(self.get_client(cluster_id))
                self._summary_managers[cluster_id] = manager
            return manager

    def reset_connections(self, cluster_id: str | None = None) -> None:
        with self._lock:
            if cluster_id is not None:
                client = self._clients.get(self.validate_cluster_id(cluster_id))
                if client is not None:
                    client.reset_connections()
                return
            for client in self._clients.values():
                client.reset_connections()


class ClusterTargetResolver:
    """Resolve a cluster from an explicit id, MCP session, or configured default."""

    def __init__(self, cluster_manager: ClusterManager):
        self.cluster_manager = cluster_manager
        self._session_clusters: dict[str, str] = {}
        self._lock = threading.Lock()

    def get_session_cluster(self, session_id: Optional[str]) -> str | None:
        if session_id:
            with self._lock:
                selected = self._session_clusters.get(session_id)
            if selected:
                return selected
        try:
            return self.cluster_manager.resolve_cluster_id()
        except ClusterSelectionError:
            return None

    def set_session_cluster(self, session_id: str, cluster_id: str | None) -> str | None:
        with self._lock:
            if cluster_id is None:
                self._session_clusters.pop(session_id, None)
            else:
                self.cluster_manager.validate_cluster_id(cluster_id)
                self._session_clusters[session_id] = cluster_id
        return self.get_session_cluster(session_id)

    def resolve(self, session_id: Optional[str], cluster_id: str | None = None) -> str:
        if cluster_id:
            return self.cluster_manager.validate_cluster_id(cluster_id)
        if session_id:
            with self._lock:
                selected = self._session_clusters.get(session_id)
            if selected:
                return selected
        return self.cluster_manager.resolve_cluster_id()


_cluster_manager_instance: ClusterManager | None = None


def get_cluster_manager() -> ClusterManager:
    """Get the process-wide cluster catalog and lazy client manager."""
    global _cluster_manager_instance
    if _cluster_manager_instance is None:
        _cluster_manager_instance = ClusterManager()
    return _cluster_manager_instance

