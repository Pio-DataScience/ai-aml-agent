"""A modular, SOLID-compliant report persistence system.

Provides date-partitioned storage for escalation and execution audits.
"""

from abc import ABC, abstractmethod
from datetime import datetime
import os
import logging
from typing import Final

logger = logging.getLogger(__name__)


class ReportPersister(ABC):
    """Abstract interface defining the contract for report persistence."""

    @abstractmethod
    def persist(self, prefix: str, content: str) -> str:
        """Persists the content and returns the path to the saved resource.

        Args:
            prefix (str): Identifier or prefix to use in the naming scheme.
            content (str): The text/markdown content to save.

        Returns:
            str: Reference identifier or path to the saved resource.
        """
        pass


class DatePartitionedFilePersister(ReportPersister):
    """Persists reports to local disk with dynamic date-based partitioning.

    Saves files under base_dir/YYYY/MM/DD/prefix_timestamp.md to prevent directory size bloating
    and make archival/organization highly scalable.
    """

    def __init__(self, base_dir: str = "./artifacts/escalations") -> None:
        """Initializes the persister with a base storage directory.

        Args:
            base_dir (str): Base folder where reports will be written.
        """
        self.base_dir: Final[str] = base_dir

    def persist(self, prefix: str, content: str) -> str:
        """Writes content to a date-partitioned file path.

        Args:
            prefix (str): Prefix for the filename (e.g. scenario code).
            content (str): The markdown report content to save.

        Returns:
            str: The path to the saved file.
        """
        now = datetime.utcnow()
        # Partition directory: YYYY/MM/DD
        partition_path = os.path.join(
            self.base_dir,
            now.strftime("%Y"),
            now.strftime("%m"),
            now.strftime("%d")
        )
        
        try:
            os.makedirs(partition_path, exist_ok=True)
            timestamp = now.strftime("%H%M%S")
            safe_prefix = str(prefix).replace("/", "_").replace("\\", "_").strip()
            filename = f"{safe_prefix}_{timestamp}.md"
            full_path = os.path.join(partition_path, filename)
            
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
                
            logger.info("[PERSISTER] Report saved successfully to %s", full_path)
            return full_path
        except Exception as exc:
            logger.error("[PERSISTER] Failed to persist report: %s", exc, exc_info=True)
            raise exc
