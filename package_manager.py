"""
package_manager.py - Centralized startup dependency verification.
Reads core dependencies exclusively from pyproject.toml (SSOT).
Execution occurs strictly at application startup; zero mid-workflow pip calls.
"""

from __future__ import annotations

import ast
import importlib.metadata
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent


def _parse_dependencies_fallback(pyproject_path: Path) -> List[str]:
    """Minimal stdlib parser for [project].dependencies when tomllib/tomli are unavailable."""
    raw = pyproject_path.read_text(encoding="utf-8")
    in_project = False
    collecting = False
    buf: List[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            collecting = False
            continue
        if in_project and stripped.startswith("dependencies") and "=" in stripped:
            rhs = stripped.split("=", 1)[1].strip()
            buf.append(rhs)
            collecting = not rhs.endswith("]")
            if not collecting:
                break
        elif collecting:
            buf.append(stripped)
            if stripped.endswith("]"):
                break
    if not buf:
        return []
    parsed = ast.literal_eval(" ".join(buf))
    return [str(x) for x in parsed if isinstance(x, str)]


def _load_tomllib_optional():
    """Loads stdlib tomllib (3.11+) or tomli if already installed."""
    try:
        import tomllib  # type: ignore[import-not-found]
        return tomllib
    except ModuleNotFoundError:
        try:
            import tomli  # type: ignore[import-not-found]
            return tomli
        except ModuleNotFoundError:
            return None


def _strip_markers_and_version(spec: str) -> tuple[str, bool]:
    """Splits environment markers and extracts clean package name."""
    parts = spec.split(";", 1)
    pkg_part = parts[0].strip()
    applicable = True
    if len(parts) == 2:
        marker = parts[1].strip()
        if "python_version < '3.11'" in marker or 'python_version < "3.11"' in marker:
            applicable = sys.version_info < (3, 11)
    clean_name = re.split(r"[><=!~\[]", pkg_part, maxsplit=1)[0].strip()
    return clean_name, applicable


class PackageManager:
    _cached_core_deps: Optional[List[str]] = None

    @classmethod
    def get_core_dependencies(cls) -> List[str]:
        """Reads core dependencies directly from pyproject.toml."""
        if cls._cached_core_deps is not None:
            return cls._cached_core_deps

        pyproject_file = APP_ROOT / "pyproject.toml"
        if not pyproject_file.exists():
            logger.warning("[PackageManager] %s not found.", pyproject_file)
            return []

        tomllib = _load_tomllib_optional()
        if tomllib is not None:
            with open(pyproject_file, "rb") as f:
                data = tomllib.load(f)
            raw_deps: List[str] = data.get("project", {}).get("dependencies", [])
        else:
            raw_deps = _parse_dependencies_fallback(pyproject_file)
        filtered: List[str] = []
        for spec in raw_deps:
            _, applicable = _strip_markers_and_version(spec)
            if applicable:
                filtered.append(spec.split(";", 1)[0].strip())

        cls._cached_core_deps = filtered
        return cls._cached_core_deps

    @classmethod
    def find_missing_packages(cls, package_specs: List[str]) -> List[str]:
        """Verifies installed packages via importlib.metadata."""
        missing: List[str] = []
        for spec in package_specs:
            clean_name, applicable = _strip_markers_and_version(spec)
            if not applicable or not clean_name:
                continue
            try:
                importlib.metadata.version(clean_name)
            except importlib.metadata.PackageNotFoundError:
                missing.append(spec.split(";", 1)[0].strip())
        return missing

    @classmethod
    def install_packages(cls, packages: List[str]) -> bool:
        """Installs missing packages upfront via pip during startup bootstrap."""
        if not packages:
            return True
        logger.info("[PackageManager] Installing missing dependencies: %s", ", ".join(packages))
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *packages])
            logger.info("[PackageManager] Dependencies installed successfully.")
            return True
        except subprocess.CalledProcessError as exc:
            logger.error("[PackageManager] Failed installing packages: %s", exc)
            return False

    @classmethod
    def ensure_core_dependencies(cls, auto_install: bool = True) -> bool:
        """Startup gate: verifies and installs missing core dependencies."""
        core_deps = cls.get_core_dependencies()
        missing = cls.find_missing_packages(core_deps)
        if not missing:
            return True
        if auto_install:
            return cls.install_packages(missing)
        return False
