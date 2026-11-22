import contextlib
import errno
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import packs
from portable import lock, print_json
from checks import validators
from store import ordinary_path


HISTORY = ".hub-store/history"
STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
LINKS = ".hub-store/links.json"
KIND_NAMES = {"skill": "skills", "rule": "rules", "mcp": "mcp_servers", "mcp_server": "mcp_servers",
              "routine": "routines", "plugin": "plugins", "connection": "connections",
              "subagent": "subagents", "command": "commands"}
SINGULAR = {"skills": "skill", "rules": "rule", "mcp_servers": "mcp_server", "routines": "routine",
            "plugins": "plugin", "connections": "connection", "subagents": "subagent", "commands": "command"}


def collection_name(value):
    value = KIND_NAMES.get(value, value)
    if value not in validators.COLLECTIONS:
        raise ValueError(f"unknown kind {value!r}; use one of {', '.join(validators.COLLECTIONS)}")
    return value


def rows(hub, root):
    shipped = packs.visible(root, validators.shipped(root, hub.read_json))
    local = validators.read_local(root, hub.read_json)
    result = []
    for source, value in (("shipped", shipped), ("imported", local)):
        for collection, key in validators.COLLECTIONS.items():
            for entry in value.get(collection, []):
                result.append({"kind": collection, "name": entry[key], "owner": entry["owner"],
                               "origin": entry["provenance"]["origin"], "source": source})
    order = list(validators.COLLECTIONS)
    return sorted(result, key=lambda row: (order.index(row["kind"]), row["name"]))


def list_items(args, hub):
    found = [row for row in rows(hub, hub.ROOT) if args.kind in (None, row["kind"])]
    if args.json:
        print_json({"items": found})
        return 0
    if not found:
        print("No items")
        return 0
    header = {"kind": "KIND", "name": "NAME", "owner": "OWNER", "origin": "ORIGIN", "source": "SOURCE"}
    table = [header, *found]
    widths = {field: max(len(row[field]) for row in table) for field in header}
    for row in table:
        print("  ".join(row[field].ljust(widths[field]) for field in header).rstrip())
    return 0


def confirmed(args, question):
    if args.yes:
        return True
    validators.require(sys.stdin.isatty(), "confirmation needed: re-run with --yes when not at a terminal")
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        raise ValueError("confirmation needed: re-run with --yes when input is unavailable") from None


def changed_targets(operations):
    return [target for target, staged in operations
            if target.exists() and (staged is None or validators.digest(target) != validators.digest(staged))]


def history_base(root, collection, item_name):
    base = root / HISTORY / collection / item_name
    ordinary_path(base)
    for directory in (base, *base.parents):
        validators.require(not directory.exists() or directory.is_dir(), "history parent must be a directory")
    return base


def archives(base):
    if not base.exists():
        return []
    return sorted(path for path in base.iterdir()
                  if re.fullmatch(r"[0-9]{8}T[0-9]{12}Z", path.name)
                  and not path.is_symlink() and path.is_dir())


def preview_history(args, hub):
    base = history_base(hub.ROOT, args.kind, validators.name(args.name))
    history = archives(base)
    if not history:
        print("No saved copies")
    for path in history[:-1]:
        print(f"Would prune {path.name}")
    if history:
        print(f"Keep {history[-1].name}")
    return 0


def copy_history(catalog, paths, destination):
    relative = {path.relative_to(catalog) for path in paths}

    def excluded(directory, names):
        parent = Path(directory).relative_to(catalog)
        return [name for name in names if not any(
            (parent / name).is_relative_to(path) or path.is_relative_to(parent / name)
            for path in relative)]

    shutil.copytree(catalog, destination, ignore=excluded)
    validators.require(all((destination / path).exists() for path in relative),
                       "could not save complete history: catalog content is missing")


def out_of_space(error):
    if isinstance(error, shutil.Error):
        # copytree reports nested errors as (source, destination, str(OSError)) triples.
        return any(reason.startswith((f"[Errno {errno.ENOSPC}]", "[WinError 112]", "[WinError 39]"))
                   for _, _, reason in error.args[0])
    return error.errno == errno.ENOSPC


def keep(root, base, paths, cleanup, rollback):
    history = archives(base)
    expired = history[:-1]
    stamp = datetime.now(timezone.utc)
    if history and stamp.strftime(STAMP_FORMAT) <= history[-1].name:
        stamp = datetime.strptime(history[-1].name, STAMP_FORMAT) + timedelta(microseconds=1)
    archive = base / stamp.strftime(STAMP_FORMAT)
    created = []

    def remove_empty_directories():
        for directory in reversed(created):
            with contextlib.suppress(OSError):
                directory.rmdir()

    rollback.callback(remove_empty_directories)
    directory = root
    for part in base.relative_to(root).parts:
        directory = directory / part
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            created.append(directory)

    def stage():
        pending = cleanup.enter_context(tempfile.TemporaryDirectory(prefix=".pending-", dir=base))
        copy = Path(pending) / "copy"
        copy_history(root / validators.LOCAL, paths, copy)
        return copy

    try:
        copy = stage()
    except OSError as error:
        if not expired or not out_of_space(error):
            raise
        cleanup.close()
        for path in expired:
            shutil.rmtree(path)
        expired.clear()
        copy = stage()

    def commit():
        copy.rename(archive)
        rollback.callback(shutil.rmtree, archive, ignore_errors=True)

    return commit, expired


@contextlib.contextmanager
def retained(root, collection, item_name, paths, *, operation="update"):
    paths = list(paths)
    expired = []
    with ordinary_path(root / "manifest.json").open("rb") as stream, contextlib.ExitStack() as rollback:
        # Windows byte-range locks must leave the shipped manifest readable during publication.
        lock(stream, offset=1 << 40)
        with contextlib.ExitStack() as cleanup:
            commit = None
            if paths:
                base = history_base(root, collection, item_name)
                try:
                    commit, expired = keep(root, base, paths, cleanup, rollback)
                except OSError as error:
                    raise ValueError(f"could not save {operation} history under {HISTORY}/: {error}") from error
            yield commit
            rollback.pop_all()
        for path in expired:
            try:
                shutil.rmtree(path)
            except OSError as error:
                print(f"Warning: could not prune {path}: {error}", file=sys.stderr)


def read_links(root):
    path = root / LINKS
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return [item for item in value["links"] if isinstance(item, dict) and set(item) == {"link", "skill"}
                and all(isinstance(field, str) for field in item.values())]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def record_links(hub, root, created):
    if not created:
        return
    current = {item["link"]: item for item in read_links(root)}
    for link, skill in created:
        current[str(link)] = {"link": str(link), "skill": str(skill)}
    hub.ordinary_parents((root / LINKS).parent)
    hub.ordinary_file(root / LINKS)
    hub.private_store(root)
    packs.pin(root)
    hub.write_json(root / LINKS, {"version": 1, "links": sorted(current.values(), key=lambda item: item["link"])})


def dangling_links(root, skill_path):
    return [item["link"] for item in read_links(root)
            if item["skill"] == str(skill_path) and Path(item["link"]).is_symlink()]


def remove(args, hub, root=None, expected=None):
    root = hub.ROOT if root is None else root
    collection = collection_name(args.kind)
    name = validators.name(args.name)
    key = validators.COLLECTIONS[collection]
    singular = SINGULAR[collection]
    shipped = validators.shipped(root, hub.read_json)
    validators.require(not any(item[key] == name for item in shipped.get(collection, [])),
                       f"{name} is a shipped {singular}; shipped items cannot be removed")
    hub.ordinary_file(root / "manifest.json")
    hub.ordinary_file(root / validators.LOCAL / "manifest.json")
    catalog = root / validators.LOCAL
    snapshots = {path: hub.import_snapshot(path) for path in (root / "manifest.json", catalog / "manifest.json")}
    value = validators.read_local(root, hub.read_json)
    entry = next((item for item in value.get(collection, []) if item[key] == name), None)
    validators.require(entry is not None, f"no imported {singular} named {name}; run hub.py list")
    validators.require(expected is None or (entry["provenance"]["origin"], entry["sha256"]) == expected,
                       f"{singular} {name} changed after its pack installed it; left in place")
    owner = validators.label(args.owner or "coldlocker")
    validators.require(validators.same_owner(entry["owner"], owner),
                       f"{name} is owned by {entry['owner']}; only that owner may remove it (--owner {entry['owner']})")
    destination = catalog / entry["path"]
    snapshots[destination] = hub.import_snapshot(destination)
    hub.import_preflight(value, collection, name, destination, entry["path"], owner,
                         entry["provenance"]["origin"], singular if singular in ("skill", "routine") else "file")
    targets = [destination]
    if singular == "routine":
        prompt = hub.read_json(destination).get("prompt")
        sibling = destination.parent / prompt if isinstance(prompt, str) else None
        if sibling is not None and len(Path(prompt).parts) == 1 and (sibling.exists() or sibling.is_symlink()):
            hub.ordinary_file(sibling)
            validators.require(sibling.is_file(), "prompt must be an ordinary file")
            targets.append(sibling)
    skill_links = dangling_links(root, destination) if singular == "skill" else []
    print(f"Remove {singular.replace('_', ' ')} {name} (owner {entry['owner']}, origin {entry['provenance']['origin']}); "
          f"a copy is kept under {HISTORY}/")
    for link in skill_links:
        print(f"Dangling link (created by hub.py link, left in place): {link}")
    if not confirmed(args, "Remove it?"):
        print("Cancelled; nothing changed")
        return 1
    snapshots.update({path: hub.import_snapshot(path) for path in targets if path not in snapshots})
    with tempfile.TemporaryDirectory() as temporary:
        staged_root = Path(temporary)
        remaining = [item for item in value[collection] if item[key] != name]
        staged_manifest = staged_root / "catalog" / "manifest.json"
        staged_manifest.parent.mkdir(parents=True)
        hub.write_json(staged_manifest, validators.manifest({**value, collection: remaining}))
        operations = [(path, None) for path in targets]
        if singular == "mcp_server":
            snapshots[root / "config.json"] = hub.import_snapshot(root / "config.json")
            if name in hub._read_bindings(root)["servers"]:
                operations.extend(hub.stage_path_bindings(root, staged_root, name, {}, True, snapshots,
                                                          hub.read_json(destination)))
        operations.append((catalog / "manifest.json", staged_manifest))
        history = retained(root, collection, name, (path for path in targets if path.exists()), operation="removal")
        hub.publish_import(operations, snapshots, history=history)
    print(f"Removed {collection}: {name}")
    if singular == "routine":
        left = sorted(item.name for item in destination.parent.iterdir())
        if left:
            print(f"Left in place (not created by import): {', '.join(left)} in {destination.parent}")
        else:
            destination.parent.rmdir()
    if skill_links:
        print("Delete the dangling links yourself; hub.py never removes files in other directories.")
    return 0


def register(commands, hub):
    command = commands.add_parser("list", help="List shipped and imported items with kind, name, owner, origin and source.")
    command.add_argument("--kind", type=collection_name, choices=list(validators.COLLECTIONS), metavar="KIND",
                         help="Only this kind: " + ", ".join(validators.COLLECTIONS) + ".")
    command.add_argument("--json", action="store_true", help="Print the rows as JSON.")
    command.set_defaults(action=lambda args: list_items(args, hub))
    command = commands.add_parser("history", help="Preview which saved copies the next changed import or removal would prune.")
    command.add_argument("kind", type=collection_name, metavar="KIND")
    command.add_argument("name")
    command.set_defaults(action=lambda args: preview_history(args, hub))
    command = commands.add_parser(
        "remove", help="Remove an imported item and its catalog entry; shipped items are refused.",
        description="Remove an imported item. Deletes its files and local manifest entry with the same staged, "
                    "rollback-safe publish as import, keeps a copy under .hub-store/history/, refuses shipped items "
                    "and items owned by another owner, and reports links made by hub.py link that would now dangle.")
    command.add_argument("kind", type=collection_name, metavar="KIND",
                         help="One of: " + ", ".join(validators.COLLECTIONS) + " (singular names also work).")
    command.add_argument("name")
    command.add_argument("--owner", help="The owner acting on the item (default coldlocker); must match the item's owner.")
    command.add_argument("--yes", action="store_true", help="Remove without asking; required when not at a terminal.")
    command.set_defaults(action=lambda args: remove(args, hub))
