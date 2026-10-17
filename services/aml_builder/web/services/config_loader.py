"""Validated, mtime-aware loading for the four AML YAML catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


CatalogStatus = Literal["demo", "confirmed"]


class CatalogModel(BaseModel):
    """Shared top-level catalog metadata."""

    model_config = ConfigDict(extra="allow")

    version: int = Field(ge=1)
    status: CatalogStatus


class ParamsCatalog(CatalogModel):
    """Scenario parameter catalog."""

    params: dict[str, dict[str, Any]]


class FilterDimension(BaseModel):
    """One configurable filter dimension."""

    model_config = ConfigDict(extra="allow")

    label: str
    level: str
    target: dict[str, Any]
    resolver: str
    status: Literal["confirmed", "needs_advisor_input"]

    @model_validator(mode="after")
    def reject_enabled_tbd_values(self) -> "FilterDimension":
        """Reject placeholder values unless the dimension is disabled.

        Returns:
            FilterDimension: The validated dimension.

        Raises:
            ValueError: If an enabled dimension contains a TBD value.
        """
        if self.status != "needs_advisor_input" and _contains_tbd(
            self.model_dump(mode="python")
        ):
            raise ValueError(
                "TBD values are allowed only when status is needs_advisor_input"
            )
        return self


class FiltersCatalog(CatalogModel):
    """Filter dimension catalog."""

    dimensions: dict[str, FilterDimension]


class PolicyCatalog(CatalogModel):
    """Authorization and conflict policy catalog."""

    model_config = ConfigDict(extra="allow")

    roles: dict[str, list[str]]
    users: dict[str, str]
    default_role: str
    cross_user_chat_read: bool
    chat_may_deploy: bool
    auth_mode: Literal["dev", "jwt"]
    relations: dict[str, str]
    similar_min_score: float = Field(ge=0, le=1)
    max_matches_shown: int = Field(ge=1)
    dimension_weights: dict[str, int]
    reason_required_for: list[str]


class UiCopyCatalog(BaseModel):
    """User-facing static notices and tips."""

    model_config = ConfigDict(extra="allow")

    version: int = Field(default=1, ge=1)
    status: CatalogStatus = "demo"
    locked_chat_tip: str
    locked_composer_placeholder: str
    conflict_block: str
    conflict_warn: str


@dataclass(frozen=True)
class Catalogs:
    """Validated values from all four catalog files."""

    params: ParamsCatalog
    filters: FiltersCatalog
    policy: PolicyCatalog
    ui_copy: UiCopyCatalog


class CatalogLoader:
    """Load catalog models and refresh them when source mtimes change."""

    _FILES: dict[str, tuple[str, type[BaseModel]]] = {
        "params": ("params.yaml", ParamsCatalog),
        "filters": ("filters.yaml", FiltersCatalog),
        "policy": ("policy.yaml", PolicyCatalog),
        "ui_copy": ("ui_copy.yaml", UiCopyCatalog),
    }

    def __init__(self, directory: Path) -> None:
        """Initialize a loader for one catalog directory.

        Args:
            directory (Path): Directory containing the four YAML catalogs.
        """
        self.directory = Path(directory)
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._catalogs: Catalogs | None = None

    def load(self) -> Catalogs:
        """Return the current catalogs, revalidating when an mtime changes.

        Returns:
            Catalogs: Validated models for all four catalog files.

        Raises:
            ValueError: If a file is missing, invalid YAML, or fails validation.
        """
        signature = self._file_signature()
        if self._catalogs is not None and signature == self._signature:
            return self._catalogs

        loaded: dict[str, BaseModel] = {}
        for name, (filename, model_type) in self._FILES.items():
            path = self.directory / filename
            try:
                with path.open("r", encoding="utf-8") as stream:
                    raw_data = yaml.safe_load(stream)
                if not isinstance(raw_data, dict):
                    raise ValueError("catalog root must be a YAML mapping")
                loaded[name] = model_type.model_validate(raw_data)
            except (OSError, yaml.YAMLError, ValueError) as exc:
                raise ValueError(f"Invalid AML catalog {path}: {exc}") from exc
            except Exception as exc:
                raise ValueError(f"Invalid AML catalog {path}: {exc}") from exc

        catalogs = Catalogs(
            params=loaded["params"],
            filters=loaded["filters"],
            policy=loaded["policy"],
            ui_copy=loaded["ui_copy"],
        )
        self._catalogs = catalogs
        self._signature = signature
        return catalogs

    def _file_signature(self) -> tuple[tuple[str, int, int], ...]:
        """Return file names, mtimes and sizes for cache invalidation.

        Returns:
            tuple[tuple[str, int, int], ...]: Stable file signature.

        Raises:
            ValueError: If any expected catalog file is missing.
        """
        signature: list[tuple[str, int, int]] = []
        for filename, _model in self._FILES.values():
            path = self.directory / filename
            try:
                stat = path.stat()
            except OSError as exc:
                raise ValueError(f"Missing AML catalog {path}: {exc}") from exc
            signature.append((filename, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)


_loader: CatalogLoader | None = None


def load_catalogs(directory: Path | str = Path("config/aml_catalogs")) -> Catalogs:
    """Load the configured AML catalogs through the process-level cache.

    Args:
        directory (Path | str): Catalog directory.

    Returns:
        Catalogs: Current validated catalogs.
    """
    global _loader
    resolved = Path(directory)
    if _loader is None or _loader.directory != resolved:
        _loader = CatalogLoader(resolved)
    return _loader.load()


def _contains_tbd(value: Any) -> bool:
    """Return whether any nested string is the TBD placeholder.

    Args:
        value (Any): Arbitrarily nested catalog data.

    Returns:
        bool: True if a string equals TBD, ignoring case and whitespace.
    """
    if isinstance(value, str):
        return value.strip().casefold() == "tbd"
    if isinstance(value, dict):
        return any(_contains_tbd(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_tbd(item) for item in value)
    return False
