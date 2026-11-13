"""Pinned, hash-verified voice resource management."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_MANIFEST = Path(__file__).resolve().parent.parent / "config" / "voice-resources.json"
PACKAGED_MANIFEST = Path(__file__).resolve().parent / "data" / "voice-resources.json"
MANIFEST = PROJECT_MANIFEST if PROJECT_MANIFEST.is_file() else PACKAGED_MANIFEST


class ResourceError(RuntimeError):
    pass


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("resources"), list):
            raise ValueError("unsupported manifest schema")
        ids: set[str] = set()
        for item in value["resources"]:
            required = {"id", "purpose", "version", "architectures", "url", "size_bytes", "sha256", "archive", "install_dir", "license"}
            if not isinstance(item, dict) or not required <= item.keys():
                raise ValueError("resource entry is incomplete or has duplicate id")
            if any(not isinstance(item[key], str) or not item[key] for key in ("id", "purpose", "version", "url", "install_dir", "license")):
                raise ValueError("resource entry contains an invalid string field")
            if item["id"] in ids:
                raise ValueError("resource entry is incomplete or has duplicate id")
            ids.add(item["id"])
            if not isinstance(item["architectures"], list) or not item["architectures"] or any(not isinstance(value, str) or not value for value in item["architectures"]):
                raise ValueError(f"invalid architectures for {item['id']}")
            _validate_relpath(item["install_dir"])
            if "filename" in item:
                _validate_relpath(item["filename"])
            if not item["url"].startswith("https://") or item["archive"] not in {"tar.bz2", "tar.gz", "zip", "file"}:
                raise ValueError(f"invalid source/archive for {item['id']}")
            if not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0 or len(item["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in item["sha256"]):
                raise ValueError(f"invalid size/hash for {item['id']}")
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ResourceError(f"Invalid resource manifest {path}: {exc}") from exc


def _validate_relpath(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or chr(92) in value or ":" in path.parts[0]:
        raise ResourceError(f"Unsafe relative path: {value!r}")
    return path


def default_root() -> Path:
    return Path("build") / "linux-aarch64" / "voice" / "resources"


def _resources(manifest: dict[str, Any], ids: list[str] | None) -> list[dict[str, Any]]:
    selected = [item for item in manifest["resources"] if ids is None or item["id"] in ids]
    missing = set(ids or ()) - {item["id"] for item in selected}
    if missing:
        raise ResourceError(f"Unknown resource id(s): {', '.join(sorted(missing))}")
    if not selected:
        raise ResourceError("No resources selected")
    return selected


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _check(path: Path, item: dict[str, Any]) -> None:
    if not path.is_file():
        raise ResourceError(f"Missing archive: {path}")
    digest, size = _digest(path)
    if size != item["size_bytes"] or digest != item["sha256"]:
        raise ResourceError(f"Size or SHA-256 mismatch for {item['id']}")


def _archive_path(item: dict[str, Any]) -> str:
    return item["id"] + "." + ("bin" if item["archive"] == "file" else item["archive"])


def download(manifest: dict[str, Any], root: Path, ids: list[str] | None = None) -> None:
    cache = root / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    for item in _resources(manifest, ids):
        target = cache / _archive_path(item)
        if target.exists():
            try:
                _check(target, item)
                print(f"Verified cached {item['id']}")
                continue
            except ResourceError:
                pass
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
        try:
            with os.fdopen(fd, "wb") as output, urllib.request.urlopen(item["url"], timeout=60) as response:
                shutil.copyfileobj(response, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            _check(Path(temporary), item)
            os.replace(temporary, target)
            print(f"Downloaded {item['id']}")
        finally:
            Path(temporary).unlink(missing_ok=True)


def _safe_destination(root: Path, member: str) -> Path:
    rel = _validate_relpath(member)
    dest = root.joinpath(*rel.parts)
    resolved_root = root.resolve()
    if not dest.resolve().is_relative_to(resolved_root):
        raise ResourceError(f"Archive path escapes destination: {member}")
    current = resolved_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ResourceError(f"Symbolic link in destination path: {member}")
    return dest


def _extract(archive: Path, fmt: str, destination: Path, filename: str | None = None) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if fmt.startswith("tar"):
        mode = {"tar.bz2": "r:bz2", "tar.gz": "r:gz"}[fmt]
        with tarfile.open(archive, mode) as bundle:
            members = bundle.getmembers()
            for member in members:
                _safe_destination(destination, member.name)
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ResourceError(f"Unsupported link or special tar member: {member.name}")
            for member in members:
                target = _safe_destination(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ResourceError(f"Cannot read archive member {member.name}")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(stat.S_IMODE(member.mode) & 0o755)
    elif fmt == "zip":
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = _safe_destination(destination, member.filename.rstrip("/"))
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ResourceError(f"Unsupported link or special zip member: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
    elif fmt == "file":
        target = _safe_destination(destination, filename or archive.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, target)
    else:
        raise ResourceError(f"Unsupported archive format: {fmt}")


def install(manifest: dict[str, Any], root: Path, ids: list[str] | None = None) -> None:
    selected = _resources(manifest, ids)
    staged: list[tuple[Path, Path, dict[str, Any]]] = []
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for item in selected:
            archive = root / "downloads" / _archive_path(item)
            _check(archive, item)
            target = _safe_destination(root, item["install_dir"])
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f".{item['id']}-", dir=target.parent))
            staged.append((stage, target, item))
            _extract(archive, item["archive"], stage, item.get("filename"))

        for stage, target, item in staged:
            backup = target.with_name(target.name + ".previous")
            if backup.exists():
                raise ResourceError(f"Refusing to overwrite recovery directory {backup}")
            if target.exists():
                os.replace(target, backup)
                backups.append((backup, target))
            os.replace(stage, target)
            installed.append(target)

    except Exception:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        for backup, target in reversed(backups):
            os.replace(backup, target)
        raise
    finally:
        for stage, _target, _item in staged:
            if stage.exists():
                shutil.rmtree(stage)
    for backup, _target in backups:
        try:
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Warning: could not remove previous resource backup {backup}: {exc}")
    for _stage, _target, item in staged:
        print(f"Installed {item['id']}")


def verify(manifest: dict[str, Any], root: Path, ids: list[str] | None = None) -> None:
    for item in _resources(manifest, ids):
        archive = root / "downloads" / _archive_path(item)
        _check(archive, item)
        destination = _safe_destination(root, item["install_dir"])
        if not destination.is_dir() or not any(destination.iterdir()):
            raise ResourceError(f"Installed resource is missing or empty: {item['id']}")
        with tempfile.TemporaryDirectory(prefix=".verify-") as temporary:
            staged = Path(temporary) / "resource"
            _extract(archive, item["archive"], staged, item.get("filename"))
            expected_files = {path.relative_to(staged).as_posix(): path for path in staged.rglob("*") if path.is_file()}
            installed_files = {path.relative_to(destination).as_posix(): path for path in destination.rglob("*") if path.is_file()}
            if any(path.is_symlink() for path in destination.rglob("*")) or expected_files.keys() != installed_files.keys():
                raise ResourceError(f"Installed resource contents do not match archive: {item['id']}")
            for relative, expected in expected_files.items():
                if _digest(expected) != _digest(installed_files[relative]):
                    raise ResourceError(f"Installed resource file mismatch for {item['id']}: {relative}")
        print(f"Verified {item['id']}")


def pack(manifest: dict[str, Any], root: Path, output: Path, ids: list[str] | None = None) -> None:
    selected = _resources(manifest, ids)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".voice-resources-", suffix=".zip", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            subset_manifest = {"schema_version": manifest["schema_version"], "resources": selected}
            bundle.writestr("manifest.json", json.dumps(subset_manifest, ensure_ascii=False, indent=2))
            for item in selected:
                archive = root / "downloads" / _archive_path(item)
                _check(archive, item)
                bundle.write(archive, "archives/" + archive.name)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def import_bundle(bundle_path: Path, manifest: dict[str, Any], root: Path, ids: list[str] | None = None) -> None:
    selected = _resources(manifest, ids)
    cache = root / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path) as bundle:
        names = set(bundle.namelist())
        embedded = json.loads(bundle.read("manifest.json"))
        if not isinstance(embedded, dict) or embedded.get("schema_version") != manifest.get("schema_version") or not isinstance(embedded.get("resources"), list):
            raise ResourceError("Offline bundle manifest has an unsupported schema")
        embedded_items = embedded["resources"]
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in embedded_items):
            raise ResourceError("Offline bundle manifest contains an invalid resource entry")
        try:
            for item in embedded_items:
                required = {"id", "purpose", "version", "architectures", "url", "size_bytes", "sha256", "archive", "install_dir", "license"}
                if not required <= item.keys():
                    raise ValueError("resource entry is incomplete")
                _validate_relpath(item["install_dir"])
                if "filename" in item:
                    _validate_relpath(item["filename"])
                if not item["url"].startswith("https://") or item["archive"] not in {"tar.bz2", "tar.gz", "zip", "file"}:
                    raise ValueError("resource URL or archive format is invalid")
                if not isinstance(item["size_bytes"], int) or item["size_bytes"] <= 0 or len(item["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in item["sha256"]):
                    raise ValueError("resource size or hash is invalid")
        except (KeyError, TypeError, ValueError, ResourceError) as exc:
            raise ResourceError(f"Offline bundle manifest contains an invalid resource: {exc}") from exc
        expected = {item["id"]: item for item in embedded_items}
        if len(expected) != len(embedded_items):
            raise ResourceError("Offline bundle manifest contains duplicate resource IDs")
        selected_by_id = {item["id"]: item for item in selected}
        if set(expected) != set(selected_by_id) or any(expected[key] != item for key, item in selected_by_id.items()):
            raise ResourceError("Offline manifest does not match the selected pinned resources")
        staged: list[tuple[Path, Path]] = []
        try:
            for item in selected:
                archive_name = "archives/" + _archive_path(item)
                if archive_name not in names:
                    raise ResourceError(f"Offline bundle is missing {item['id']}")
                info = bundle.getinfo(archive_name)
                if info.file_size != item["size_bytes"]:
                    raise ResourceError(f"Offline archive size mismatch for {item['id']}")
                fd, temporary = tempfile.mkstemp(prefix=".import-", dir=cache)
                try:
                    with os.fdopen(fd, "wb") as out, bundle.open(archive_name) as source:
                        shutil.copyfileobj(source, out)
                    _check(Path(temporary), item)
                    staged.append((Path(temporary), cache / _archive_path(item)))
                finally:
                    if not any(source == Path(temporary) for source, _ in staged):
                        Path(temporary).unlink(missing_ok=True)
            backups: list[tuple[Path, Path]] = []
            installed: list[Path] = []
            try:
                for source, target in staged:
                    backup = target.with_name(target.name + f".import-{os.getpid()}.previous")
                    if backup.exists():
                        raise ResourceError(f"Refusing to overwrite recovery file {backup}")
                    if target.exists():
                        os.replace(target, backup)
                        backups.append((backup, target))
                    os.replace(source, target)
                    installed.append(target)
            except Exception:
                for target in reversed(installed):
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                for backup, target in reversed(backups):
                    os.replace(backup, target)
                raise
            for backup, _ in backups:
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink(missing_ok=True)
        finally:
            for source, _ in staged:
                source.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="robotsoul voice-resources")
    parser.add_argument("action", choices=("download", "install", "verify", "pack", "import"))
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--id", dest="ids", action="append")
    parser.add_argument("--output", type=Path, help="Offline bundle path for pack")
    parser.add_argument("--bundle", type=Path, help="Offline bundle path for import")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.action == "download":
            download(manifest, args.root, args.ids)
        elif args.action == "install":
            install(manifest, args.root, args.ids)
        elif args.action == "verify":
            verify(manifest, args.root, args.ids)
        elif args.action == "pack":
            if not args.output:
                parser.error("pack requires --output")
            pack(manifest, args.root, args.output, args.ids)
        else:
            if not args.bundle:
                parser.error("import requires --bundle")
            import_bundle(args.bundle, manifest, args.root, args.ids)
    except (ResourceError, OSError, zipfile.BadZipFile, tarfile.TarError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
