import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from portable import lock
from store import ordinary_path


DEFINITIONS = "packs.json"
STATE = ".hub-store/packs.json"
EARLIER_INSTALL = (".hub-store/catalog/manifest.json", ".hub-store/links.json", ".hub-store/calls.jsonl")
INSTALLED = ".hub-store/installed-packs.json"
BUNDLE = "coldlocker-pack.json"
BUNDLE_KINDS = {"skill": "skills", "rule": "rules", "mcp": "mcp_servers"}
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(ordinary_path(path).read_text(encoding="utf-8"))


def definitions(root):
    value = read(Path(root) / DEFINITIONS)
    require(isinstance(value, dict) and set(value) == {"schema_version", "always", "packs"}
            and value["schema_version"] == 1, "invalid packs.json fields")
    groups = [value["always"], *value["packs"].values()] if isinstance(value["packs"], dict) else []
    require(groups and all(isinstance(group, list) and all(isinstance(name, str) for name in group) for group in groups),
            "packs.json lists must hold skill names")
    return value["always"], value["packs"]


def covered(root, shipped):
    shipped_names = [entry["name"] for entry in shipped.get("skills", [])]
    if not ordinary_path(Path(root) / DEFINITIONS).exists():
        return shipped_names, {}
    always, packs = definitions(root)
    policy_pack = ordinary_path(Path(root) / "policy-pack.json")
    if policy_pack.exists():
        packs = {**packs, "pstack": packs.get("pstack", []) + [entry["name"] for entry in read(policy_pack)["skills"]]}
    members = always + [name for names in packs.values() for name in names]
    require(len(members) == len(set(members)) and set(members) == set(shipped_names),
            "packs.json must list every shipped skill exactly once")
    return always, packs


def enabled(root, packs):
    path = ordinary_path(Path(root) / STATE)
    if not path.exists():
        earlier = any(ordinary_path(Path(root) / marker).exists() for marker in EARLIER_INSTALL)
        return sorted(packs) if earlier else []
    value = read(path)
    require(isinstance(value, dict) and set(value) == {"version", "enabled"} and value["version"] == 1
            and isinstance(value["enabled"], list) and value["enabled"] == sorted(set(value["enabled"]))
            and set(value["enabled"]) <= set(packs),
            f"invalid {STATE}; fix or delete it, then run python3 hub.py pack list")
    return value["enabled"]


def visible(root, shipped):
    always, packs = covered(root, shipped)
    keep = set(always).union(*(packs[name] for name in enabled(root, packs)))
    return {**shipped, "skills": [entry for entry in shipped.get("skills", []) if entry["name"] in keep]}


def save(root, names, replace=True):
    write(root, STATE, {"version": 1, "enabled": sorted(set(names))}, replace)


def write(root, relative, value, replace=True):
    path = ordinary_path(Path(root) / relative)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".packs-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
    finally:
        Path(temporary).unlink(missing_ok=True)


def pin(root):
    if ordinary_path(Path(root) / DEFINITIONS).exists() and not ordinary_path(Path(root) / STATE).exists():
        save(root, enabled(root, definitions(root)[1]), replace=False)


def change(args, hub):
    root = hub.ROOT
    _, packs = covered(root, hub.validators.shipped(root, hub.read_json))
    require(args.name in packs, f"unknown pack {args.name}; run python3 hub.py pack list")
    current = set(enabled(root, packs))
    wanted = current | {args.name} if args.action_name == "add" else current - {args.name}
    if wanted == current:
        print(f"Pack {args.name} is already {'on' if args.name in current else 'off'}; nothing changed")
        return 0
    save(root, wanted)
    count = len(packs[args.name])
    if args.action_name == "add":
        print(f"Added pack {args.name}: {count} shipped skills are listed again")
        return 0
    print(f"Removed pack {args.name}: its {count} shipped skills are no longer listed; their files stay in skills/")
    removed = {str(root / "skills" / name) for name in packs[args.name]}
    for link in (item["link"] for item in hub.lifecycle.read_links(root) if item["skill"] in removed):
        print(f"Link left in place (created by hub.py link): {link}")
    return 0


def show(args, hub):
    root = hub.ROOT
    always, packs = covered(root, hub.validators.shipped(root, hub.read_json))
    on = set(enabled(root, packs))
    print(f"Always listed: {', '.join(always)}")
    for name in sorted(packs):
        print(f"{name}  {'on' if name in on else 'off'}  {len(packs[name])} skills")
    return 0


def installed(root):
    path = ordinary_path(Path(root) / INSTALLED)
    if not path.exists():
        return {}
    value = read(path)
    require(isinstance(value, dict) and set(value) == {"version", "packs"} and value["version"] == 1
            and isinstance(value["packs"], dict), f"invalid {INSTALLED}")
    return value["packs"]


@contextlib.contextmanager
def installing(hub, root):
    hub.private_store(root)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(Path(root) / ".hub-store/installed-packs.lock", flags, 0o600), "r+b") as stream:
        lock(stream)
        yield installed(root)


def inside(checkout, relative):
    parts = relative.split("/")
    refused = f"REFUSED: pack entry path {relative!r} must be a plain relative path inside the pack"
    require("\\" not in relative and ":" not in relative and not {"", ".", ".."} & set(parts), refused)
    current = checkout
    for part in parts:
        current = current / part
        require(not current.is_symlink(), refused + "; symlinks are not followed")
    require(current.exists(), f"pack entry {relative} is missing from the repository")
    return current


def bundle(checkout, validators):
    path = checkout / BUNDLE
    require(path.is_file() and not path.is_symlink(), f"the repository has no {BUNDLE} at its root")
    value = read(path)
    require(isinstance(value, dict) and set(value) == {"schema_version", "name", "entries"}
            and value["schema_version"] == 1 and isinstance(value["entries"], list) and value["entries"],
            f"invalid {BUNDLE}: needs schema_version 1, name and a nonempty entries list")
    name = validators.name(value["name"])
    require(len(name) <= 58, "pack name must be at most 58 characters")
    entries = []
    for entry in value["entries"]:
        require(isinstance(entry, dict) and {"kind", "path"} <= set(entry) <= {"kind", "path", "sha256"}
                and entry["kind"] in BUNDLE_KINDS and isinstance(entry["path"], str)
                and (entry.get("sha256") is None or isinstance(entry["sha256"], str)),
                f"invalid {BUNDLE} entry {entry!r}: needs kind ({', '.join(BUNDLE_KINDS)}), path and optional sha256")
        entries.append((entry, inside(checkout, entry["path"])))
    paths = [entry["path"] for entry, _ in entries]
    require(len(paths) == len(set(paths)), f"{BUNDLE} lists a path twice")
    return name, entries


def identity(hub, kind, source):
    if kind == "skill":
        return "skills", hub.validators.name(source.name)
    if kind == "rule":
        return "rules", hub.validators.name(source.stem.lower())
    value = hub.read_json(source)
    return "mcp_servers", hub.validators.name(value.get("name") if isinstance(value, dict) else None)


def record(root, current, name, value):
    if value is None:
        current.pop(name, None)
    else:
        current[name] = value
    write(root, INSTALLED, {"version": 1, "packs": current})


DESTINATIONS = {"skills": "skills/{}", "rules": "rules/{}.md", "mcp_servers": "mcp/servers/{}.json"}


def remove_installed(hub, root, current, name, recovering=False):
    value = current[name]
    catalog = root / hub.validators.LOCAL
    removed = 0
    for item in reversed(list(value["installed"])):
        local = hub.validators.read_local(root, hub.read_json)
        entry = next((found for found in local.get(item["kind"], []) if found["name"] == item["name"]), None)
        expected = ("pack-" + name, item["sha256"] or (entry or {}).get("sha256"))
        destination = catalog / DESTINATIONS[item["kind"]].format(item["name"])
        if entry is None and item["sha256"] is None:
            if recovering and (destination.exists() or destination.is_symlink()):
                print(f"Kept in the pack record: {destination} is unindexed, left by an interrupted install; "
                      f"move it aside, then run python3 hub.py pack uninstall {name} --yes again")
                continue
        elif entry is None or (entry["provenance"]["origin"], entry["sha256"]) != expected:
            print(f"Left in place: {item['kind']} {item['name']} changed or was removed after pack {name} installed it")
        elif not (catalog / entry["path"]).exists():
            print(f"Left in place: {item['kind']} {item['name']} files were deleted locally; its catalog entry remains")
        elif hub.validators.digest(catalog / entry["path"]) != entry["sha256"]:
            print(f"Left in place: {item['kind']} {item['name']} has local edits; copy them out, then run "
                  f"python3 hub.py remove {item['kind']} {item['name']} --yes")
        else:
            hub.lifecycle.remove(argparse.Namespace(kind=item["kind"], name=item["name"], owner=None, yes=True),
                                 hub, root, expected=expected)
            removed += 1
        value["installed"].remove(item)
        record(root, current, name, value)
    return removed


def install(args, hub):
    with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
        result = install_pack(args, hub)
    if args.json:
        hub.print_json(result)
    elif "name" in result:
        held = [item for item in result["entries"] if item["status"] == "held"]
        for item in held:
            print(f"Held for review: {item['path']}: {item['reason']}")
        print(f"Installed pack {result['name']} at {result['commit']}: "
              f"{len(result['entries']) - len(held)} installed, {len(held)} held for review")
        if held:
            print("Held entries were not imported. To import a reviewed untrusted entry, uninstall the pack and install it "
                  "again with --approve-risky <path>; unpinned or mismatched entries need a corrected pin in the pack.")
    return result["exit_code"]


def install_pack(args, hub):
    root = hub.ROOT
    owner, repo, path, ref = hub.github_source(args.repo)
    require(not path, "pack install takes the repository root: https://github.com/<owner>/<repo>@<commit>")
    require(ref is not None and COMMIT.fullmatch(ref),
            "pin the pack to a full 40-character commit SHA (https://github.com/<owner>/<repo>@<commit>); "
            "branches and tags can move under the pack's pins")
    if not args.yes and not sys.stdin.isatty():
        print("Confirmation needed: re-run pack install with --yes when not at a terminal.", file=sys.stderr)
        return {"exit_code": 1, "entries": [], "error": "confirmation required"}
    if not hub.lifecycle.confirmed(args, f"Install the pinned pack from {owner}/{repo}?"):
        print("Cancelled; nothing changed")
        return {"exit_code": 1, "entries": [], "error": "cancelled"}
    url = f"https://github.com/{owner}/{repo}"
    with installing(hub, root) as current, tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / repo
        commit = hub.fetch_github(owner, repo, ref, checkout)
        require(commit == ref, f"fetched commit {commit} does not match the pinned {ref}")
        name, entries = bundle(checkout, hub.validators)
        require(name not in current, f"pack {name} is already installed; run python3 hub.py pack uninstall {name} --yes first")
        local = hub.validators.read_local(root, hub.read_json)
        held, ready, seen = [], [], set()
        installed_by_path = {}
        for entry, source in entries:
            kind = entry["kind"]
            actual = hub.validators.digest(source)
            approve = entry["path"] in args.approve_risky
            if entry.get("sha256") is None:
                held.append({"path": entry["path"], "reason": f"unpinned: the pack must give its sha256 ({actual})"})
                continue
            if entry["sha256"] != actual:
                held.append({"path": entry["path"], "reason": f"pin mismatch: content sha256 is {actual}"})
                continue
            collection, item_name = identity(hub, kind, source)
            require((collection, item_name) not in seen, f"pack {name} lists {collection} {item_name} twice")
            seen.add((collection, item_name))
            require(all(found["name"] != item_name for found in local.get(collection, [])),
                    f"{collection} {item_name} already exists in the catalog; remove it before installing pack {name}")
            try:
                hub.screen(kind, hub.read_json(source) if kind == "mcp" else None,
                           hub.skill_package_texts(source) if kind == "skill" else hub.texts(source),
                           approve, root, name=item_name, record=False)
            except hub.Refused as refusal:
                held.append({"path": entry["path"], "reason": str(refusal)})
                continue
            ready.append((entry, source, collection, item_name, approve))
        value = {"url": url, "commit": commit, "installed": [], "held": held}
        record(root, current, name, value)
        try:
            for entry, source, collection, item_name, approve in ready:
                pending = {"kind": collection, "name": item_name, "sha256": None}
                value["installed"].append(pending)
                record(root, current, name, value)
                try:
                    hub.import_item(argparse.Namespace(command=f"import-{entry['kind']}", path=str(source), name=None,
                                                       owner=None, origin="pack-" + name, approve_risky=approve,
                                                       create_only=True), root)
                except hub.Refused as refusal:
                    value["installed"].remove(pending)
                    held.append({"path": entry["path"], "reason": str(refusal)})
                    record(root, current, name, value)
                    continue
                pending["sha256"] = next(found for found in hub.validators.read_local(root, hub.read_json)[collection]
                                         if found["name"] == item_name)["sha256"]
                record(root, current, name, value)
                installed_by_path[entry["path"]] = pending
        except BaseException:
            try:
                remove_installed(hub, root, current, name)
            except Exception as error:
                raise ValueError(f"pack {name} failed and its rollback is incomplete ({error}); fix that, then run "
                                 f"python3 hub.py pack uninstall {name} --yes") from error
            record(root, current, name, None)
            raise
    outcomes = {item["path"]: {"status": "held", "reason": item["reason"]} for item in held}
    outcomes.update({path: {"status": "installed", "name": item["name"], "sha256": item["sha256"]}
                     for path, item in installed_by_path.items()})
    return {"name": name, "commit": commit, "exit_code": int(bool(held)),
            "entries": [{"path": entry["path"], "kind": entry["kind"], **outcomes[entry["path"]]}
                        for entry, _ in entries]}


def uninstall(args, hub):
    root = hub.ROOT
    name = hub.validators.name(args.name)
    with installing(hub, root) as current:
        require(name in current, f"pack {name} is not installed; installed: {', '.join(sorted(current)) or 'none'}")
        if not hub.lifecycle.confirmed(args, f"Uninstall pack {name} and every item it installed?"):
            print("Cancelled; nothing changed")
            return 1
        removed = remove_installed(hub, root, current, name, recovering=True)
        require(not current[name]["installed"], f"pack {name} is only partly uninstalled: removed {removed} items")
        record(root, current, name, None)
    print(f"Uninstalled pack {name}: removed {removed} items")
    return 0


def register(commands, hub):
    command = commands.add_parser(
        "pack", help="List, add or remove packs of shipped skills.",
        description="Shipped skills come in opt-in packs. A new install lists only hub-discover and your own imports. "
                    "An install that already had imports, links or MCP calls before packs existed keeps every pack on. "
                    "Adding or removing a pack changes only what is listed and linked; no skill files are deleted.")
    actions = command.add_subparsers(dest="action_name", required=True)
    actions.add_parser("list", help="Show each pack and whether it is on.").set_defaults(action=lambda args: show(args, hub))
    for name, text in (("add", "List a pack's skills."), ("remove", "Stop listing a pack's skills.")):
        action = actions.add_parser(name, help=text)
        action.add_argument("name")
        action.set_defaults(action=lambda args: change(args, hub))
    action = actions.add_parser("install", help="Install a pinned bundle of skills, rules and MCP definitions from GitHub.",
                                description=f"Fetch https://github.com/<owner>/<repo>@<40-character commit> and import each "
                                            f"entry its {BUNDLE} pins by sha256, through the same safety scan as import. "
                                            "Unpinned, mismatched or flagged entries are held for review; a failed "
                                            "import removes the entries already installed.")
    action.add_argument("repo", metavar="URL")
    action.add_argument("--yes", action="store_true", help="Confirm install; required when not at a terminal.")
    action.add_argument("--json", action="store_true", help="Print versioned per-entry results.")
    action.add_argument("--approve-risky", action="append", default=[], metavar="PATH",
                        help="Import this pinned entry even though the safety scan flagged it. Repeatable.")
    action.set_defaults(action=lambda args: install(args, hub))
    action = actions.add_parser("uninstall", help="Remove every item an installed pack imported.")
    action.add_argument("name")
    action.add_argument("--yes", action="store_true", help="Uninstall without asking; required when not at a terminal.")
    action.set_defaults(action=lambda args: uninstall(args, hub))
