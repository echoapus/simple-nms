"""Small helpers for MIB file discovery and module-name handling."""

import os
import re
from collections.abc import Iterable

MIB_SOURCE_EXTS = (".mib", ".my", ".txt")
MIB_LOAD_EXTS = MIB_SOURCE_EXTS + (".py", "")
MODULE_RE = re.compile(r"^([A-Za-z0-9-]+)\s+DEFINITIONS\s*::=\s*BEGIN", re.IGNORECASE)


def parse_module_name_from_lines(lines: Iterable[str]) -> str | None:
    for line in lines:
        line = line.split("--")[0].strip()
        if not line:
            continue
        match = MODULE_RE.match(line)
        if match:
            return match.group(1)
    return None


def parse_module_name(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_module_name_from_lines(f.readline() for _ in range(1000))
    except OSError:
        return None


def first_writable_dir(dirs: Iterable[str], default: str | None = None) -> str | None:
    for d in dirs:
        if os.path.exists(d):
            if os.stat(d).st_mode & 0o222 and os.access(d, os.W_OK):
                return d
        else:
            parent = os.path.dirname(d) or "."
            if os.path.exists(parent) and os.stat(parent).st_mode & 0o222 and os.access(parent, os.W_OK):
                return d
    return default


def discover_modules(dirs: Iterable[str], exts=MIB_LOAD_EXTS) -> list[str]:
    modules = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            path = os.path.join(d, entry)
            if not os.path.isfile(path):
                continue
            name, ext = os.path.splitext(entry)
            if ext.lower() not in exts:
                continue
            module_name = name if ext.lower() == ".py" else parse_module_name(path) or name
            if module_name and module_name[0].isalpha() and all(c.isalnum() or c in ("-", "_") for c in module_name):
                modules.append(module_name)
    return modules


def list_mib_files(dirs: Iterable[str]) -> list[dict]:
    files = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            path = os.path.join(d, entry)
            name, ext = os.path.splitext(entry)
            if os.path.isfile(path) and not os.path.islink(path) and ext.lower() in MIB_SOURCE_EXTS:
                files.append({
                    "filename": entry,
                    "module_name": parse_module_name(path) or name,
                    "size": os.path.getsize(path),
                })
    return files


def remove_module_symlinks(directory: str, filename: str, keep: str | None = None) -> None:
    for entry in os.listdir(directory):
        if entry == keep:
            continue
        path = os.path.join(directory, entry)
        try:
            target = os.readlink(path)
        except OSError:
            continue
        if target == filename:
            os.remove(path)


def ensure_module_symlink(directory: str, filename: str, module_name: str) -> None:
    name_base, ext = os.path.splitext(filename)
    if module_name == name_base:
        remove_module_symlinks(directory, filename)
        return
    link_name = f"{module_name}{ext}"
    link_path = os.path.join(directory, link_name)
    remove_module_symlinks(directory, filename, keep=link_name)
    if not os.path.exists(link_path):
        os.symlink(filename, link_path)
