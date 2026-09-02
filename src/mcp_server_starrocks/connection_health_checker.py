# Copyright 2021-present StarRocks, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
import time
from loguru import logger


class ConnectionHealthChecker:
    """
    A singleton class that manages database connection health monitoring.
    """
    
    def __init__(self, db_client, check_interval=30):
        """
        Initialize the connection health checker.
        
        Args:
            db_client: Database client instance for health checks
            check_interval: Health check interval in seconds (default: 30)
        """
        self.db_client = db_client
        self.check_interval = check_interval
        self._health_check_thread = None
        self._health_check_stop_event = threading.Event()
        self._last_connection_status = None
        self._last_healthy_log = None
    
    def check_connection_health(self):
        """
        Check database connection health by executing a simple query.
        Returns tuple of (is_healthy: bool, error_message: str or None)
        """
        try:
            result = self.db_client.execute("show databases")
            if result.success:
                return True, None
            else:
                return False, result.error_message
        except Exception as e:
            return False, str(e)

    def _connection_health_checker_loop(self):
        """
        Background thread function that periodically checks connection health.
        """
        logger.info(f"Starting connection health checker (interval: {self.check_interval}s)")
        while True:
            is_healthy, error_msg = self.check_connection_health()
            # Log status changes or periodic status updates
            if self._last_connection_status != is_healthy:
                if is_healthy:
                    logger.info("Database connection is healthy")
                else:
                    logger.warning(f"Database connection is unhealthy: {error_msg}")
            else:
                # Log periodic status (every 5 minutes when healthy, every check when unhealthy)
                current_time = time.time()
                if is_healthy:
                    if self._last_healthy_log is None:
                        self._last_healthy_log = current_time
                    elif current_time - self._last_healthy_log >= 300:  # 5 minutes
                        logger.info("Database connection remains healthy")
                        self._last_healthy_log = current_time
                else:
                    logger.warning(f"Database connection remains unhealthy: {error_msg}")
            self._last_connection_status = is_healthy
            # Wait for interval or stop event
            if self._health_check_stop_event.wait(self.check_interval):
                break
        logger.info("Connection health checker stopped")

    def start(self):
        """
        Start the connection health checker thread.
        """
        if self._health_check_thread is None or not self._health_check_thread.is_alive():
            self._health_check_stop_event.clear()
            self._health_check_thread = threading.Thread(
                target=self._connection_health_checker_loop,
                name="ConnectionHealthChecker",
                daemon=True
            )
            self._health_check_thread.start()
            logger.info("Connection health checker thread started")

    def stop(self):
        """
        Stop the connection health checker thread.
        """
        if self._health_check_thread is not None:
            self._health_check_stop_event.set()
            self._health_check_thread.join(timeout=5)
            if self._health_check_thread.is_alive():
                logger.warning("Connection health checker thread did not stop gracefully")
            else:
                logger.info("Connection health checker thread stopped")
            self._health_check_thread = None


class ClusterConnectionHealthChecker:
    """Monitor initialized clients without eagerly connecting every cluster."""

    def __init__(self, cluster_manager, check_interval=30):
        self.cluster_manager = cluster_manager
        self.check_interval = check_interval
        self._health_check_thread = None
        self._health_check_stop_event = threading.Event()
        self._last_connection_status = {}

    def check_connection_health(self, cluster_id=None):
        if cluster_id is not None:
            client = self.cluster_manager.get_client(cluster_id)
            result = client.execute("show databases")
            return (True, None) if result.success else (False, result.error_message)

        statuses = {}
        for initialized_id in self.cluster_manager.initialized_cluster_ids:
            statuses[initialized_id] = self.check_connection_health(initialized_id)
        return statuses

    def _connection_health_checker_loop(self):
        logger.info(f"Starting cluster connection health checker (interval: {self.check_interval}s)")
        while True:
            statuses = self.check_connection_health()
            for cluster_id, (is_healthy, error_msg) in statuses.items():
                previous = self._last_connection_status.get(cluster_id)
                if previous != is_healthy:
                    if is_healthy:
                        logger.info(f"Database connection is healthy: cluster={cluster_id}")
                    else:
                        logger.warning(
                            f"Database connection is unhealthy: cluster={cluster_id}: {error_msg}"
                        )
                self._last_connection_status[cluster_id] = is_healthy
            if self._health_check_stop_event.wait(self.check_interval):
                break
        logger.info("Cluster connection health checker stopped")

    def start(self):
        if self._health_check_thread is None or not self._health_check_thread.is_alive():
            self._health_check_stop_event.clear()
            self._health_check_thread = threading.Thread(
                target=self._connection_health_checker_loop,
                name="ClusterConnectionHealthChecker",
                daemon=True,
            )
            self._health_check_thread.start()

    def stop(self):
        if self._health_check_thread is not None:
            self._health_check_stop_event.set()
            self._health_check_thread.join(timeout=5)
            self._health_check_thread = None


# Global instance - will be initialized in server.py
_health_checker_instance = None


def initialize_health_checker(db_client=None, check_interval=30, cluster_manager=None):
    """
    Initialize the global connection health checker instance.
    
    Args:
        db_client: Database client instance
        check_interval: Health check interval in seconds
    """
    global _health_checker_instance
    if cluster_manager is not None:
        _health_checker_instance = ClusterConnectionHealthChecker(
            cluster_manager, check_interval
        )
    else:
        _health_checker_instance = ConnectionHealthChecker(db_client, check_interval)
    return _health_checker_instance


def start_connection_health_checker():
    """
    Start the connection health checker thread.
    """
    if _health_checker_instance is None:
        raise RuntimeError("Health checker not initialized. Call initialize_health_checker() first.")
    _health_checker_instance.start()


def stop_connection_health_checker():
    """
    Stop the connection health checker thread.
    """
    if _health_checker_instance is not None:
        _health_checker_instance.stop()


def check_connection_health(cluster_id=None):
    """
    Check database connection health by executing a simple query.
    Returns tuple of (is_healthy: bool, error_message: str or None)
    """
    if _health_checker_instance is None:
        raise RuntimeError("Health checker not initialized. Call initialize_health_checker() first.")
    if isinstance(_health_checker_instance, ClusterConnectionHealthChecker):
        return _health_checker_instance.check_connection_health(cluster_id)
    return _health_checker_instance.check_connection_health()
