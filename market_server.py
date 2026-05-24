from __future__ import annotations

import asyncio
import ast
import json
import mimetypes
import os
import re
import subprocess
import threading
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
INDEX_FILE_NAMES = (
    "market_tree.json",
    "plugin_ids_map.json",
    "latest_versions.json",
    "directory_tree.json",
    "directory.json",
)
IGNORED_DIR_NAMES = {".git", "__pycache__"}

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 24011,
    },
    "markets": {
        "official_root": "./PluginMarket",
        "third_party_root": "./ThirdPartyMarket",
        "third_party_suffix": "-third",
        "greetings": "§a欢迎使用 §bToolDelta 插件市场.",
        "official_source_name": "ToolDelta插件市场 Official",
        "third_party_source_name": "ThirdPluginMarket",
        "third_party_market_version": "0.1.0",
        "official_greetings": "§a欢迎使用 §bToolDelta 插件市场.",
        "third_party_greetings": "§a欢迎使用第三方插件市场.",
    },
    "sync": {
        "official_git_enabled": True,
        "official_check_interval_seconds": 300,
        "third_party_scan_interval_seconds": 60,
        "git_timeout_seconds": 30,
    },
    "storage": {
        "build_dir": "./build",
        "log_dir": "./logs",
    },
}


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(payload), encoding="utf-8")


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve(strict=False)


def append_suffix_once(value: str, suffix: str) -> str:
    if value.endswith(suffix):
        return value
    return f"{value}{suffix}"


def unique_name(candidate: str, existing: set[str]) -> str:
    if candidate not in existing:
        existing.add(candidate)
        return candidate
    index = 2
    while True:
        value = f"{candidate}-{index}"
        if value not in existing:
            existing.add(value)
            return value
        index += 1


def is_ignored_directory(name: str) -> bool:
    return name.startswith(".") or name in IGNORED_DIR_NAMES


def split_virtual_path(path_value: str) -> list[str]:
    normalized = path_value.replace("\\", "/").strip("/")
    if not normalized:
        return []
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"", "/"}]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("unsafe path")
    return parts


def safe_resolve_path(root: Path, relative_parts: list[str]) -> Path:
    target = root.joinpath(*relative_parts).resolve()
    root_resolved = root.resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError("unsafe path")
    return target


def build_directory_tree(root: Path) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_dir():
            if is_ignored_directory(child.name):
                continue
            tree[child.name] = build_directory_tree(child)
        else:
            tree[child.name] = 0
    return tree


def build_directory_index(root: Path, virtual_root: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = sorted(
            [name for name in dir_names if not is_ignored_directory(name)]
        )
        current_path = Path(current_root)
        relative = current_path.relative_to(root)
        index_key = virtual_root if relative == Path(".") else f"{virtual_root}/{relative.as_posix()}"
        index[index_key] = sorted(file_names)
    return index


def group_plugins_for_market(plugins: list["PluginScan"]) -> list["PluginScan"]:
    main_plugins = sorted(
        (plugin for plugin in plugins if "前置" not in plugin.actual_root_name),
        key=lambda item: item.actual_root_name,
    )
    dependency_plugins = sorted(
        (plugin for plugin in plugins if "前置" in plugin.actual_root_name),
        key=lambda item: item.actual_root_name,
    )
    return main_plugins + dependency_plugins


def service_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class AppConfig:
    host: str
    port: int
    official_root: Path
    third_party_root: Path
    third_party_suffix: str
    greetings: str
    official_source_name: str
    third_party_source_name: str
    third_party_market_version: str
    official_greetings: str
    third_party_greetings: str
    official_git_enabled: bool
    official_check_interval_seconds: int
    third_party_scan_interval_seconds: int
    git_timeout_seconds: int
    build_dir: Path
    log_dir: Path


def load_config(config_path: Path = CONFIG_PATH) -> AppConfig:
    if not config_path.exists():
        write_json(config_path, DEFAULT_CONFIG)
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    merged = deep_merge(DEFAULT_CONFIG, loaded)
    if merged != loaded:
        write_json(config_path, merged)

    server = merged["server"]
    markets = merged["markets"]
    sync = merged["sync"]
    storage = merged["storage"]
    return AppConfig(
        host=str(server["host"]),
        port=int(server["port"]),
        official_root=resolve_path(str(markets["official_root"])),
        third_party_root=resolve_path(str(markets["third_party_root"])),
        third_party_suffix=str(markets["third_party_suffix"]),
        greetings=str(markets["greetings"]),
        official_source_name=str(markets["official_source_name"]),
        third_party_source_name=str(markets["third_party_source_name"]),
        third_party_market_version=str(markets["third_party_market_version"]),
        official_greetings=str(markets["official_greetings"]),
        third_party_greetings=str(markets["third_party_greetings"]),
        official_git_enabled=bool(sync["official_git_enabled"]),
        official_check_interval_seconds=max(10, int(sync["official_check_interval_seconds"])),
        third_party_scan_interval_seconds=max(10, int(sync["third_party_scan_interval_seconds"])),
        git_timeout_seconds=max(5, int(sync["git_timeout_seconds"])),
        build_dir=resolve_path(str(storage["build_dir"])),
        log_dir=resolve_path(str(storage["log_dir"])),
    )


@dataclass(slots=True)
class PluginScan:
    actual_root_name: str
    actual_root_path: Path
    plugin_id: str
    author: str
    version: str
    plugin_type: str


@dataclass(slots=True)
class PackageScan:
    actual_root_name: str
    actual_root_path: Path
    package_name: str
    plugin_ids: list[str]
    description: str
    author: str
    version: str


@dataclass(slots=True)
class SourceScan:
    source_key: str
    root: Path
    source_name: str
    market_version: str
    greetings: str
    plugins: list[PluginScan] = field(default_factory=list)
    packages: list[PackageScan] = field(default_factory=list)


@dataclass(slots=True)
class SourceIndexes:
    market_tree: dict[str, Any]
    plugin_ids_map: dict[str, str]
    latest_versions: dict[str, str]
    directory_tree: dict[str, Any]
    directory: dict[str, list[str]]


@dataclass(slots=True)
class RootRoute:
    source_key: str
    kind: str
    actual_root_name: str
    actual_root_path: Path
    virtual_root_name: str
    actual_plugin_id: str | None = None
    virtual_plugin_id: str | None = None


@dataclass(slots=True)
class MarketSnapshot:
    built_at: str
    merged_market_tree: dict[str, Any]
    merged_plugin_ids_map: dict[str, str]
    merged_latest_versions: dict[str, str]
    merged_directory_tree: dict[str, Any]
    merged_directory: dict[str, list[str]]
    root_routes: dict[str, RootRoute]
    third_party_plugin_id_map: dict[str, str]
    third_party_root_name_map: dict[str, str]
    official_indexes: SourceIndexes
    third_party_indexes: SourceIndexes
    official_plugin_count: int
    official_package_count: int
    third_party_plugin_count: int
    third_party_package_count: int


class RequestLogger:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._counts = Counter()
        self._lock = threading.Lock()
        self._load_counts()

    def _load_counts(self) -> None:
        ip_pattern = re.compile(r"\bip=(?P<ip>\S+)\b")
        for log_path in sorted(self.log_dir.glob("*.log")):
            try:
                with log_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        match = ip_pattern.search(line)
                        if match:
                            self._counts[match.group("ip")] += 1
            except OSError:
                continue

    def log(self, ip: str, repo: str, file_path: str, method: str, status_code: int) -> None:
        timestamp = datetime.now().astimezone()
        with self._lock:
            self._counts[ip] += 1
            count = self._counts[ip]
            log_path = self.log_dir / f"{timestamp:%Y-%m-%d}.log"
            line = (
                f"[{timestamp.isoformat(timespec='seconds')}] "
                f"ip={ip} method={method} repo={repo} file={file_path} "
                f"status={status_code} count={count}"
            )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        print(line, flush=True)


class MarketManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = RequestLogger(config.log_dir)
        self._snapshot_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._snapshot: MarketSnapshot | None = None
        self._background_tasks: list[asyncio.Task[Any]] = []
        self._stop_event: asyncio.Event | None = None

    def log(self, message: str) -> None:
        print(f"[{service_now()}] {message}", flush=True)

    def ensure_directories(self) -> None:
        self.config.build_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.config.third_party_root.mkdir(parents=True, exist_ok=True)

    def current_snapshot(self) -> MarketSnapshot:
        with self._snapshot_lock:
            if self._snapshot is None:
                raise RuntimeError("market snapshot has not been built yet")
            return self._snapshot

    def initialize(self) -> None:
        self.ensure_directories()
        self.refresh_all(sync_official_git=False)

    def refresh_all(self, sync_official_git: bool) -> None:
        with self._refresh_lock:
            self.ensure_directories()
            if sync_official_git:
                self.sync_official_repository()

            official_scan = self.scan_source(
                source_key="official",
                root=self.config.official_root,
                default_source_name=self.config.official_source_name,
                default_market_version="0.0.0",
                default_greetings=self.config.official_greetings,
            )
            third_scan = self.scan_source(
                source_key="third_party",
                root=self.config.third_party_root,
                default_source_name=self.config.third_party_source_name,
                default_market_version=self.config.third_party_market_version,
                default_greetings=self.config.third_party_greetings,
            )

            official_indexes = self.render_source_indexes(official_scan)
            third_indexes = self.render_source_indexes(third_scan)
            snapshot = self.build_merged_snapshot(
                official_scan=official_scan,
                third_scan=third_scan,
                official_indexes=official_indexes,
                third_indexes=third_indexes,
            )

            self.write_build_indexes("official", official_indexes)
            self.write_build_indexes("third_party", third_indexes)
            self.write_build_indexes(
                "merged",
                SourceIndexes(
                    market_tree=snapshot.merged_market_tree,
                    plugin_ids_map=snapshot.merged_plugin_ids_map,
                    latest_versions=snapshot.merged_latest_versions,
                    directory_tree=snapshot.merged_directory_tree,
                    directory=snapshot.merged_directory,
                ),
            )

            with self._snapshot_lock:
                self._snapshot = snapshot

            self.log(
                "market indexes refreshed "
                f"(official plugins={snapshot.official_plugin_count}, "
                f"third-party plugins={snapshot.third_party_plugin_count})"
            )

    def scan_source(
        self,
        source_key: str,
        root: Path,
        default_source_name: str,
        default_market_version: str,
        default_greetings: str,
    ) -> SourceScan:
        metadata = self.read_source_metadata(
            root=root,
            default_source_name=default_source_name,
            default_market_version=default_market_version,
            default_greetings=default_greetings,
        )
        scan = SourceScan(
            source_key=source_key,
            root=root,
            source_name=metadata["SourceName"],
            market_version=metadata["MarketVersion"],
            greetings=metadata["Greetings"],
        )
        if not root.exists():
            if source_key == "official":
                self.log(f"official market directory does not exist: {root}")
            return scan

        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or is_ignored_directory(entry.name):
                continue
            datas_path = entry / "datas.json"
            if not datas_path.is_file():
                continue
            try:
                payload = json.loads(datas_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.log(f"skip invalid datas.json: {datas_path} ({exc})")
                continue

            if entry.name.startswith("[pkg]"):
                scan.packages.append(
                    PackageScan(
                        actual_root_name=entry.name,
                        actual_root_path=entry,
                        package_name=entry.name[5:],
                        plugin_ids=list(payload.get("plugin-ids", [])),
                        description=str(payload.get("description", "")),
                        author=str(payload.get("author", "")),
                        version=str(payload.get("version", "0.0.0")),
                    )
                )
            else:
                plugin_id = payload.get("plugin-id")
                if not isinstance(plugin_id, str) or not plugin_id.strip():
                    self.log(f"skip plugin without valid plugin-id: {datas_path}")
                    continue
                scan.plugins.append(
                    PluginScan(
                        actual_root_name=entry.name,
                        actual_root_path=entry,
                        plugin_id=plugin_id,
                        author=str(payload.get("author", "")),
                        version=str(payload.get("version", "0.0.0")),
                        plugin_type=str(payload.get("plugin-type", "classic")),
                    )
                )
        return scan

    def read_source_metadata(
        self,
        root: Path,
        default_source_name: str,
        default_market_version: str,
        default_greetings: str,
    ) -> dict[str, str]:
        metadata = {
            "MarketVersion": default_market_version,
            "SourceName": default_source_name,
            "Greetings": default_greetings,
        }
        source_market_tree = root / "market_tree.json"
        if source_market_tree.is_file():
            try:
                payload = json.loads(source_market_tree.read_text(encoding="utf-8"))
            except Exception as exc:
                self.log(f"failed to read source market_tree.json: {source_market_tree} ({exc})")
                return metadata
            metadata["MarketVersion"] = str(payload.get("MarketVersion", metadata["MarketVersion"]))
            metadata["SourceName"] = str(payload.get("SourceName", metadata["SourceName"]))
            metadata["Greetings"] = str(payload.get("Greetings", metadata["Greetings"]))
        return metadata

    def render_source_indexes(self, scan: SourceScan) -> SourceIndexes:
        market_plugins: dict[str, Any] = {}
        packages: dict[str, Any] = {}
        plugin_ids_map: dict[str, str] = {}
        latest_versions: dict[str, str] = {}
        directory_tree: dict[str, Any] = {name: 0 for name in INDEX_FILE_NAMES}
        directory: dict[str, list[str]] = {}

        for plugin in group_plugins_for_market(scan.plugins):
            market_plugins[plugin.plugin_id] = {
                "name": plugin.actual_root_name,
                "author": plugin.author,
                "version": plugin.version,
                "plugin-type": plugin.plugin_type,
            }
            plugin_ids_map[plugin.plugin_id] = plugin.actual_root_name
            latest_versions[plugin.plugin_id] = plugin.version
            directory_tree[plugin.actual_root_name] = build_directory_tree(plugin.actual_root_path)
            directory.update(build_directory_index(plugin.actual_root_path, plugin.actual_root_name))

        for package in sorted(scan.packages, key=lambda item: item.package_name):
            packages[package.package_name] = {
                "plugin-ids": package.plugin_ids,
                "description": package.description,
                "author": package.author,
                "version": package.version,
            }
            directory_tree[package.actual_root_name] = build_directory_tree(package.actual_root_path)
            directory.update(build_directory_index(package.actual_root_path, package.actual_root_name))

        return SourceIndexes(
            market_tree={
                "MarketVersion": scan.market_version,
                "SourceName": scan.source_name,
                "Greetings": scan.greetings,
                "MarketPlugins": market_plugins,
                "Packages": packages,
            },
            plugin_ids_map=plugin_ids_map,
            latest_versions=latest_versions,
            directory_tree=directory_tree,
            directory=directory,
        )

    def build_merged_snapshot(
        self,
        official_scan: SourceScan,
        third_scan: SourceScan,
        official_indexes: SourceIndexes,
        third_indexes: SourceIndexes,
    ) -> MarketSnapshot:
        merged_market_plugins: dict[str, Any] = {}
        merged_packages: dict[str, Any] = {}
        merged_plugin_ids_map: dict[str, str] = {}
        merged_latest_versions: dict[str, str] = {}
        merged_directory_tree: dict[str, Any] = {name: 0 for name in INDEX_FILE_NAMES}
        merged_directory: dict[str, list[str]] = {}
        root_routes: dict[str, RootRoute] = {}
        existing_root_names = set(merged_directory_tree)
        existing_plugin_ids: set[str] = set()
        existing_package_names: set[str] = set()
        third_party_plugin_id_map: dict[str, str] = {}
        third_party_root_name_map: dict[str, str] = {}

        for plugin in group_plugins_for_market(official_scan.plugins):
            plugin_id = unique_name(plugin.plugin_id, existing_plugin_ids)
            virtual_root = unique_name(plugin.actual_root_name, existing_root_names)
            merged_market_plugins[plugin_id] = {
                "name": virtual_root,
                "author": plugin.author,
                "version": plugin.version,
                "plugin-type": plugin.plugin_type,
            }
            merged_plugin_ids_map[plugin_id] = virtual_root
            merged_latest_versions[plugin_id] = plugin.version
            merged_directory_tree[virtual_root] = build_directory_tree(plugin.actual_root_path)
            merged_directory.update(build_directory_index(plugin.actual_root_path, virtual_root))
            root_routes[virtual_root] = RootRoute(
                source_key="official",
                kind="plugin",
                actual_root_name=plugin.actual_root_name,
                actual_root_path=plugin.actual_root_path,
                virtual_root_name=virtual_root,
                actual_plugin_id=plugin.plugin_id,
                virtual_plugin_id=plugin_id,
            )

        for package in sorted(official_scan.packages, key=lambda item: item.package_name):
            package_name = unique_name(package.package_name, existing_package_names)
            virtual_root = unique_name(package.actual_root_name, existing_root_names)
            merged_packages[package_name] = {
                "plugin-ids": list(package.plugin_ids),
                "description": package.description,
                "author": package.author,
                "version": package.version,
            }
            merged_directory_tree[virtual_root] = build_directory_tree(package.actual_root_path)
            merged_directory.update(build_directory_index(package.actual_root_path, virtual_root))
            root_routes[virtual_root] = RootRoute(
                source_key="official",
                kind="package",
                actual_root_name=package.actual_root_name,
                actual_root_path=package.actual_root_path,
                virtual_root_name=virtual_root,
            )

        for plugin in group_plugins_for_market(third_scan.plugins):
            virtual_plugin_id = unique_name(
                append_suffix_once(plugin.plugin_id, self.config.third_party_suffix),
                existing_plugin_ids,
            )
            virtual_root = unique_name(
                append_suffix_once(plugin.actual_root_name, self.config.third_party_suffix),
                existing_root_names,
            )
            third_party_plugin_id_map[plugin.plugin_id] = virtual_plugin_id
            third_party_root_name_map[plugin.actual_root_name] = virtual_root
            merged_market_plugins[virtual_plugin_id] = {
                "name": virtual_root,
                "author": plugin.author,
                "version": plugin.version,
                "plugin-type": plugin.plugin_type,
            }
            merged_plugin_ids_map[virtual_plugin_id] = virtual_root
            merged_latest_versions[virtual_plugin_id] = plugin.version
            merged_directory_tree[virtual_root] = build_directory_tree(plugin.actual_root_path)
            merged_directory.update(build_directory_index(plugin.actual_root_path, virtual_root))
            root_routes[virtual_root] = RootRoute(
                source_key="third_party",
                kind="plugin",
                actual_root_name=plugin.actual_root_name,
                actual_root_path=plugin.actual_root_path,
                virtual_root_name=virtual_root,
                actual_plugin_id=plugin.plugin_id,
                virtual_plugin_id=virtual_plugin_id,
            )

        for package in sorted(third_scan.packages, key=lambda item: item.package_name):
            virtual_package_name = unique_name(
                append_suffix_once(package.package_name, self.config.third_party_suffix),
                existing_package_names,
            )
            virtual_root = unique_name(
                append_suffix_once(package.actual_root_name, self.config.third_party_suffix),
                existing_root_names,
            )
            merged_packages[virtual_package_name] = {
                "plugin-ids": [
                    third_party_plugin_id_map.get(plugin_id, plugin_id)
                    for plugin_id in package.plugin_ids
                ],
                "description": package.description,
                "author": package.author,
                "version": package.version,
            }
            merged_directory_tree[virtual_root] = build_directory_tree(package.actual_root_path)
            merged_directory.update(build_directory_index(package.actual_root_path, virtual_root))
            root_routes[virtual_root] = RootRoute(
                source_key="third_party",
                kind="package",
                actual_root_name=package.actual_root_name,
                actual_root_path=package.actual_root_path,
                virtual_root_name=virtual_root,
            )

        merged_market_tree = {
            "MarketVersion": third_scan.market_version,
            "SourceName": f"{official_scan.source_name}&{third_scan.source_name}",
            "Greetings": self.config.greetings,
            "MarketPlugins": merged_market_plugins,
            "Packages": merged_packages,
        }

        return MarketSnapshot(
            built_at=service_now(),
            merged_market_tree=merged_market_tree,
            merged_plugin_ids_map=merged_plugin_ids_map,
            merged_latest_versions=merged_latest_versions,
            merged_directory_tree=merged_directory_tree,
            merged_directory=merged_directory,
            root_routes=root_routes,
            third_party_plugin_id_map=third_party_plugin_id_map,
            third_party_root_name_map=third_party_root_name_map,
            official_indexes=official_indexes,
            third_party_indexes=third_indexes,
            official_plugin_count=len(official_scan.plugins),
            official_package_count=len(official_scan.packages),
            third_party_plugin_count=len(third_scan.plugins),
            third_party_package_count=len(third_scan.packages),
        )

    def write_build_indexes(self, name: str, indexes: SourceIndexes) -> None:
        target_dir = self.config.build_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        write_json(target_dir / "market_tree.json", indexes.market_tree)
        write_json(target_dir / "plugin_ids_map.json", indexes.plugin_ids_map)
        write_json(target_dir / "latest_versions.json", indexes.latest_versions)
        write_json(target_dir / "directory_tree.json", indexes.directory_tree)
        write_json(target_dir / "directory.json", indexes.directory)

    def sync_official_repository(self) -> None:
        if not self.config.official_git_enabled:
            return
        root = self.config.official_root
        if not root.exists():
            return
        if not self.is_git_repository(root):
            return

        fetch = self.run_git(root, ["fetch", "--all", "--prune", "--quiet"], check=False)
        if fetch.returncode != 0:
            self.log(f"git fetch failed for official market: {fetch.stderr.strip()}")
            return

        head = self.run_git(root, ["rev-parse", "HEAD"], check=False)
        upstream = self.run_git(root, ["rev-parse", "@{upstream}"], check=False)
        if head.returncode != 0 or upstream.returncode != 0:
            return
        if head.stdout.strip() == upstream.stdout.strip():
            return

        pull = self.run_git(root, ["pull", "--ff-only"], check=False)
        if pull.returncode == 0:
            self.log("official market repository updated via git pull")
        else:
            self.log(f"git pull failed for official market: {pull.stderr.strip()}")

    def is_git_repository(self, root: Path) -> bool:
        result = self.run_git(root, ["rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def run_git(
        self,
        root: Path,
        args: list[str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self.config.git_timeout_seconds,
            check=check,
        )

    def classify_request(self, path_value: str) -> tuple[str, str]:
        if path_value in {"", "/"}:
            return "system", "/"
        stripped = path_value.strip("/")
        if stripped in INDEX_FILE_NAMES:
            return "merged", f"/{stripped}"
        try:
            parts = split_virtual_path(stripped)
        except ValueError:
            return "invalid", f"/{stripped}"
        if not parts:
            return "system", "/"
        route = self.current_snapshot().root_routes.get(parts[0])
        if route is None:
            return "unknown", f"/{stripped}"
        return route.source_key, f"/{stripped}"

    def get_root_index_response(self, name: str) -> Response:
        snapshot = self.current_snapshot()
        if name == "market_tree.json":
            payload = snapshot.merged_market_tree
        elif name == "plugin_ids_map.json":
            payload = snapshot.merged_plugin_ids_map
        elif name == "latest_versions.json":
            payload = snapshot.merged_latest_versions
        elif name == "directory_tree.json":
            payload = snapshot.merged_directory_tree
        elif name == "directory.json":
            payload = snapshot.merged_directory
        else:
            raise HTTPException(status_code=404, detail="index file not found")
        return Response(content=json_dumps(payload), media_type="application/json")

    def resolve_virtual_file(self, virtual_path: str) -> tuple[RootRoute, Path, list[str]]:
        try:
            parts = split_virtual_path(virtual_path)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        if len(parts) < 2:
            raise HTTPException(status_code=404, detail="file not found")
        route = self.current_snapshot().root_routes.get(parts[0])
        if route is None:
            raise HTTPException(status_code=404, detail="file not found")
        try:
            real_path = safe_resolve_path(route.actual_root_path, parts[1:])
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        if not real_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return route, real_path, parts[1:]

    def rewrite_plugin_python_name(
        self,
        content: str,
    ) -> str:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            pattern = re.compile(
                r"(^\s*name\s*=\s*)(?P<quote>['\"])(?P<value>.+?)(?P=quote)",
                re.MULTILINE,
            )
            return pattern.sub(
                lambda match: f"{match.group(1)}{match.group('quote')}{append_suffix_once(match.group('value'), self.config.third_party_suffix)}{match.group('quote')}",
                content,
                count=1,
            )

        line_to_replace: int | None = None
        new_value: str | None = None
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            found_name = False
            found_author = False
            found_version = False
            for stmt in node.body:
                target_name: str | None = None
                value_node: ast.AST | None = None
                if isinstance(stmt, ast.Assign):
                    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                        continue
                    target_name = stmt.targets[0].id
                    value_node = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id
                    value_node = stmt.value
                if target_name == "author":
                    found_author = True
                elif target_name == "version":
                    found_version = True
                elif (
                    target_name == "name"
                    and isinstance(value_node, ast.Constant)
                    and isinstance(value_node.value, str)
                ):
                    found_name = True
                    line_to_replace = stmt.lineno
                    new_value = append_suffix_once(value_node.value, self.config.third_party_suffix)
            if found_name and found_author and found_version and line_to_replace and new_value:
                break

        if not line_to_replace or new_value is None:
            return content

        lines = content.splitlines(keepends=True)
        original_line = lines[line_to_replace - 1]
        replaced_line = re.sub(
            r"(^\s*name\s*=\s*)(?P<quote>['\"])(?P<value>.+?)(?P=quote)",
            lambda match: f"{match.group(1)}{match.group('quote')}{new_value}{match.group('quote')}",
            original_line,
            count=1,
        )
        lines[line_to_replace - 1] = replaced_line
        return "".join(lines)

    def rewrite_datas_payload(
        self,
        route: RootRoute,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = self.current_snapshot()
        copied = json.loads(json.dumps(payload, ensure_ascii=False))
        if route.kind == "plugin" and route.virtual_plugin_id:
            copied["plugin-id"] = route.virtual_plugin_id
            pre_plugins = copied.get("pre-plugins")
            if isinstance(pre_plugins, dict):
                copied["pre-plugins"] = {
                    snapshot.third_party_plugin_id_map.get(plugin_id, plugin_id): version
                    for plugin_id, version in pre_plugins.items()
                }
        elif route.kind == "package":
            plugin_ids = copied.get("plugin-ids")
            if isinstance(plugin_ids, list):
                copied["plugin-ids"] = [
                    snapshot.third_party_plugin_id_map.get(plugin_id, plugin_id)
                    for plugin_id in plugin_ids
                ]
        return copied

    def build_file_response(self, virtual_path: str, method: str) -> Response:
        route, real_path, relative_parts = self.resolve_virtual_file(virtual_path)
        relative_name = "/".join(relative_parts)

        if route.source_key == "third_party" and relative_name == "datas.json":
            payload = json.loads(real_path.read_text(encoding="utf-8"))
            payload = self.rewrite_datas_payload(route, payload)
            body = json_dumps(payload)
            return Response(
                content="" if method == "HEAD" else body,
                media_type="application/json",
            )

        if (
            route.source_key == "third_party"
            and route.kind == "plugin"
            and real_path.suffix == ".py"
        ):
            content = real_path.read_text(encoding="utf-8")
            virtual_name = self.current_snapshot().third_party_root_name_map.get(
                route.actual_root_name,
                route.virtual_root_name,
            )
            content = self.rewrite_plugin_python_name(
                content=content,
            )
            media_type = mimetypes.guess_type(real_path.name)[0] or "text/plain"
            return Response(
                content="" if method == "HEAD" else content,
                media_type=media_type,
            )

        media_type = mimetypes.guess_type(real_path.name)[0] or "application/octet-stream"
        body = b"" if method == "HEAD" else real_path.read_bytes()
        return Response(content=body, media_type=media_type)

    def get_status_payload(self) -> dict[str, Any]:
        snapshot = self.current_snapshot()
        return {
            "status": "ok",
            "built_at": snapshot.built_at,
            "official_root": str(self.config.official_root),
            "third_party_root": str(self.config.third_party_root),
            "official_plugins": snapshot.official_plugin_count,
            "official_packages": snapshot.official_package_count,
            "third_party_plugins": snapshot.third_party_plugin_count,
            "third_party_packages": snapshot.third_party_package_count,
            "merged_plugins": len(snapshot.merged_market_tree["MarketPlugins"]),
            "merged_packages": len(snapshot.merged_market_tree["Packages"]),
        }

    async def start_background_tasks(self) -> None:
        if self._stop_event is not None:
            return
        self._stop_event = asyncio.Event()
        self._background_tasks = [
            asyncio.create_task(self._official_sync_loop()),
            asyncio.create_task(self._third_party_scan_loop()),
        ]

    async def stop_background_tasks(self) -> None:
        if self._stop_event is None:
            return
        self._stop_event.set()
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._stop_event = None

    async def wait_or_stop(self, seconds: int) -> bool:
        if self._stop_event is None:
            return True
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _official_sync_loop(self) -> None:
        while True:
            if await self.wait_or_stop(self.config.official_check_interval_seconds):
                return
            try:
                await asyncio.to_thread(self.refresh_all, True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"official sync loop failed: {exc}")

    async def _third_party_scan_loop(self) -> None:
        while True:
            if await self.wait_or_stop(self.config.third_party_scan_interval_seconds):
                return
            try:
                await asyncio.to_thread(self.refresh_all, False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"third-party scan loop failed: {exc}")


config = load_config()
manager = MarketManager(config)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(manager.initialize)
    await manager.start_background_tasks()
    try:
        yield
    finally:
        await manager.stop_background_tasks()


app = FastAPI(title="ThirdPluginMarket", lifespan=lifespan)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    repo, file_path = manager.classify_request(request.url.path)
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    client_host = forwarded_for or (request.client.host if request.client else "unknown")
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        manager.logger.log(client_host, repo, file_path, request.method, 500)
        raise
    manager.logger.log(client_host, repo, file_path, request.method, status_code)
    return response


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    status = manager.get_status_payload()
    return (
        "ThirdPluginMarket\n"
        f"built_at={status['built_at']}\n"
        f"official_plugins={status['official_plugins']}\n"
        f"third_party_plugins={status['third_party_plugins']}\n"
        f"merged_plugins={status['merged_plugins']}\n"
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return manager.get_status_payload()


@app.api_route("/{index_name}", methods=["GET", "HEAD"])
async def get_root_index(index_name: str, request: Request) -> Response:
    if index_name not in INDEX_FILE_NAMES:
        raise HTTPException(status_code=404, detail="file not found")
    if request.method == "HEAD":
        response = manager.get_root_index_response(index_name)
        return Response(media_type=response.media_type)
    return manager.get_root_index_response(index_name)


@app.api_route("/{virtual_path:path}", methods=["GET", "HEAD"])
async def get_market_file(virtual_path: str, request: Request) -> Response:
    if virtual_path in INDEX_FILE_NAMES:
        return manager.get_root_index_response(virtual_path)
    return manager.build_file_response(virtual_path, request.method)


def run() -> None:
    uvicorn.run(app, host=config.host, port=config.port)
