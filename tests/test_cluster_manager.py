"""Unit tests for named StarRocks cluster routing."""

import json
import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp import Client

from src.mcp_server_starrocks.cluster_manager import (
    ClusterConfigurationError,
    ClusterManager,
    ClusterProfile,
    ClusterSelectionError,
    ClusterTargetResolver,
    load_cluster_profiles,
)
from src.mcp_server_starrocks.db_client import DBClient
from src.mcp_server_starrocks import server


MULTI_CLUSTER_ENV_KEYS = ("STARROCKS_CLUSTERS", "STARROCKS_CLUSTERS_FILE")


def test_legacy_configuration_becomes_default_profile(monkeypatch):
    for key in MULTI_CLUSTER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    profiles, default_cluster = load_cluster_profiles()

    assert tuple(profiles) == ("default",)
    assert profiles["default"].is_legacy is True
    assert default_cluster == "default"


def test_loads_multiple_profiles_and_expands_environment(monkeypatch):
    monkeypatch.delenv("STARROCKS_CLUSTERS_FILE", raising=False)
    monkeypatch.setenv("PROD_PASSWORD", "secret")
    monkeypatch.setenv(
        "STARROCKS_CLUSTERS",
        json.dumps(
            {
                "default_cluster": "prod",
                "clusters": {
                    "prod": {
                        "host": "prod-fe",
                        "user": "mcp",
                        "password": "${PROD_PASSWORD}",
                        "description": "Production analytics",
                    },
                    "staging": {"host": "staging-fe", "dummy_test": True},
                },
            }
        ),
    )

    profiles, default_cluster = load_cluster_profiles()

    assert default_cluster == "prod"
    assert profiles["prod"].settings["password"] == "secret"
    assert profiles["prod"].description == "Production analytics"
    assert "description" not in profiles["prod"].settings


def test_multiple_profiles_without_default_require_selection(monkeypatch):
    monkeypatch.delenv("STARROCKS_CLUSTERS_FILE", raising=False)
    monkeypatch.setenv(
        "STARROCKS_CLUSTERS",
        json.dumps(
            {"clusters": {"prod": {"dummy_test": True}, "stage": {"dummy_test": True}}}
        ),
    )

    profiles, default_cluster = load_cluster_profiles()
    manager = ClusterManager(profiles, default_cluster)

    assert default_cluster is None
    with pytest.raises(ClusterSelectionError, match="Multiple StarRocks clusters"):
        manager.resolve_cluster_id()


def test_missing_environment_reference_is_rejected(monkeypatch):
    monkeypatch.delenv("STARROCKS_CLUSTERS_FILE", raising=False)
    monkeypatch.delenv("MISSING_CLUSTER_PASSWORD", raising=False)
    monkeypatch.setenv(
        "STARROCKS_CLUSTERS",
        json.dumps(
            {
                "clusters": {
                    "prod": {"host": "prod-fe", "password": "${MISSING_CLUSTER_PASSWORD}"}
                }
            }
        ),
    )

    with pytest.raises(ClusterConfigurationError, match="MISSING_CLUSTER_PASSWORD"):
        load_cluster_profiles()


@pytest.mark.parametrize("cluster_id", ["", "bad id", "/prod", "x" * 65])
def test_invalid_cluster_ids_are_rejected(monkeypatch, cluster_id):
    monkeypatch.delenv("STARROCKS_CLUSTERS_FILE", raising=False)
    monkeypatch.setenv(
        "STARROCKS_CLUSTERS",
        json.dumps({"clusters": {cluster_id: {"dummy_test": True}}}),
    )

    with pytest.raises(ClusterConfigurationError, match="Cluster ids"):
        load_cluster_profiles()


def test_clients_are_lazy_and_isolated_by_cluster():
    profiles = {
        "prod": ClusterProfile("prod", {"host": "prod-fe", "dummy_test": True}),
        "stage": ClusterProfile("stage", {"host": "stage-fe", "dummy_test": True}),
    }
    manager = ClusterManager(profiles)

    assert manager.initialized_cluster_ids == ()
    prod = manager.get_client("prod")
    stage = manager.get_client("stage")

    assert manager.get_client("prod") is prod
    assert prod is not stage
    assert prod.connection_params["host"] == "prod-fe"
    assert stage.connection_params["host"] == "stage-fe"
    assert prod.connection_params["pool_name"] != stage.connection_params["pool_name"]
    assert set(manager.initialized_cluster_ids) == {"prod", "stage"}


def test_summary_managers_are_isolated_by_cluster():
    profiles = {
        "prod": ClusterProfile("prod", {"dummy_test": True}),
        "stage": ClusterProfile("stage", {"dummy_test": True}),
    }
    manager = ClusterManager(profiles)

    prod = manager.get_summary_manager("prod")
    stage = manager.get_summary_manager("stage")

    assert manager.get_summary_manager("prod") is prod
    assert prod is not stage
    assert prod.db_client.cluster_id == "prod"
    assert stage.db_client.cluster_id == "stage"


def test_target_resolver_uses_explicit_then_session_then_default():
    profiles = {
        "prod": ClusterProfile("prod", {"dummy_test": True}),
        "stage": ClusterProfile("stage", {"dummy_test": True}),
    }
    manager = ClusterManager(profiles, default_cluster_id="prod")
    resolver = ClusterTargetResolver(manager)

    assert resolver.resolve("session-a") == "prod"
    resolver.set_session_cluster("session-a", "stage")
    assert resolver.resolve("session-a") == "stage"
    assert resolver.resolve("session-a", "prod") == "prod"
    assert resolver.resolve("session-b") == "prod"
    resolver.set_session_cluster("session-a", None)
    assert resolver.resolve("session-a") == "prod"


def test_profile_db_client_does_not_inherit_legacy_connection_environment(monkeypatch):
    monkeypatch.setenv("STARROCKS_HOST", "legacy-fe")
    monkeypatch.setenv("STARROCKS_PASSWORD", "legacy-secret")

    client = DBClient(
        config={"host": "profile-fe", "user": "profile-user", "dummy_test": True},
        cluster_id="profile",
    )

    assert client.connection_params["host"] == "profile-fe"
    assert client.connection_params["password"] == ""
    assert client._get_connection_params()["password"] == ""


def test_profile_db_client_supports_its_own_password_file(tmp_path):
    password_file = tmp_path / "profile-password"
    password_file.write_text("profile-secret\n", encoding="utf-8")

    client = DBClient(
        config={
            "host": "profile-fe",
            "user": "profile-user",
            "password_file": str(password_file),
            "dummy_test": True,
        },
        cluster_id="profile",
    )

    assert client._get_connection_params()["password"] == "profile-secret"


def test_reset_connections_can_target_one_cluster():
    profiles = {
        "prod": ClusterProfile("prod", {"dummy_test": True}),
        "stage": ClusterProfile("stage", {"dummy_test": True}),
    }
    manager = ClusterManager(profiles)
    prod = manager.get_client("prod")
    stage = manager.get_client("stage")
    prod.reset_connections = MagicMock()
    stage.reset_connections = MagicMock()

    manager.reset_connections("prod")

    prod.reset_connections.assert_called_once_with()
    stage.reset_connections.assert_not_called()


def test_mcp_session_selection_routes_existing_tools(monkeypatch):
    profiles = {
        "prod": ClusterProfile("prod", {"dummy_test": True}),
        "stage": ClusterProfile("stage", {"dummy_test": True}),
    }
    manager = ClusterManager(profiles)
    resolver = ClusterTargetResolver(manager)
    monkeypatch.setattr(server, "cluster_manager", manager)
    monkeypatch.setattr(server, "target_resolver", resolver)

    async def run():
        async with Client(server.mcp) as client:
            selected = await client.call_tool(
                "set_session_cluster", {"cluster": "stage"}
            )
            query_result = await client.call_tool(
                "read_query", {"query": "SELECT 1"}
            )
            explicit_result = await client.call_tool(
                "read_query", {"query": "SELECT 1", "cluster": "prod"}
            )
            return selected, query_result, explicit_result

    selected, query_result, explicit_result = asyncio.run(run())

    assert "stage" in selected.content[0].text
    assert query_result.structured_content["cluster_id"] == "stage"
    assert explicit_result.structured_content["cluster_id"] == "prod"


def test_cluster_resource_uri_routes_without_session(monkeypatch):
    profiles = {
        "prod": ClusterProfile("prod", {"dummy_test": True}),
        "stage": ClusterProfile("stage", {"dummy_test": True}),
    }
    manager = ClusterManager(profiles)
    monkeypatch.setattr(server, "cluster_manager", manager)

    resource = asyncio.run(
        server.mcp.read_resource("starrocks:///stage/databases")
    )

    assert "aaa" in resource.contents[0].content
    assert manager.initialized_cluster_ids == ("stage",)


def test_legacy_resource_uri_uses_session_cluster(monkeypatch):
    profiles = {
        "prod": ClusterProfile("prod", {"dummy_test": True}),
        "stage": ClusterProfile("stage", {"dummy_test": True}),
    }
    manager = ClusterManager(profiles)
    resolver = ClusterTargetResolver(manager)
    monkeypatch.setattr(server, "cluster_manager", manager)
    monkeypatch.setattr(server, "target_resolver", resolver)

    async def run():
        async with Client(server.mcp) as client:
            await client.call_tool("set_session_cluster", {"cluster": "stage"})
            return await client.read_resource("starrocks:///databases")

    resource = asyncio.run(run())

    assert "aaa" in resource[0].text
    assert manager.initialized_cluster_ids == ("stage",)
