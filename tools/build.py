#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Build and validate the single-package TerraMaster TOS 7 .deb from Windows."""

from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipapp
from urllib.parse import urlparse
from xml.etree import ElementTree


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ID = "naspeed"
LANGUAGES = (
    "zh-cn", "zh-hk", "en-us", "ko-kr", "ja-jp", "de-de", "fr-fr",
    "it-it", "es-es", "hu-hu", "pl-pl", "tr-tr", "ru-ru", "pt-pt",
    "cs-cz", "da-dk", "nl-nl", "nb-no", "sv-se", "el-gr", "ro-ro",
    "th-th", "vi-vn", "id-id", "ar-sa", "he-il",
)
REQUIRED_LANGUAGES = LANGUAGES[:14]
LANGUAGE_FIELDS = ("name", "auth", "descript", "release_note", "important")
DESCRIPTION = "Multi-provider speed test for TerraMaster TOS"
ARCHIVE_ENTRY = """from naspeed.__main__ import main
raise SystemExit(main())
"""
REQUIRED_CONFIG = {
    "id": str,
    "icon": str,
    "publisher": str,
    "exec": bool,
    "type": str,
    "path": str,
    "help": str,
    "version": str,
    "recommend": bool,
    "beta": bool,
    "low_version": str,
    "category": list,
    "relation": list,
    "platform": str,
    "application_type": str,
    "system_id": str,
    "package": str,
    "user": str,
    "allow_open_in_mobile": bool,
}
SCRIPT_NAMES = ("preinst", "postinst", "prerm", "postrm")


class BuildError(Exception):
    """Raised when source or package metadata fails validation."""


def _read_version(root: pathlib.Path = ROOT) -> tuple[str, bool]:
    source = root / "src" / APP_ID / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("__version__", "BETA"):
                    values[target.id] = ast.literal_eval(node.value)
    version = values.get("__version__")
    beta = values.get("BETA")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        raise BuildError("__version__ must contain one to three numeric segments.")
    if type(beta) is not bool:
        raise BuildError("BETA must be a bool in src/naspeed/__init__.py.")
    return version, beta


def _load_metadata(root: pathlib.Path = ROOT) -> dict:
    path = root / "packaging" / "developer.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("Cannot read packaging/developer.json: %s" % exc) from exc
    if not isinstance(metadata, dict):
        raise BuildError("packaging/developer.json must contain a JSON object.")
    return metadata


def _validate_metadata(metadata: dict) -> None:
    required = ("publisher", "maintainer_name", "maintainer_email", "help_url")
    missing = [key for key in required if not isinstance(metadata.get(key), str) or not metadata[key].strip()]
    if missing:
        raise BuildError(
            "Complete packaging/developer.json before building; missing: %s."
            % ", ".join(missing)
        )
    email = metadata["maintainer_email"].strip()
    if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", email):
        raise BuildError("maintainer_email must be a valid email address.")
    if any(char in metadata["publisher"] for char in "\r\n"):
        raise BuildError("publisher must not contain line breaks.")
    for key in ("help_url", "official_url"):
        if not isinstance(metadata.get(key, ""), str):
            raise BuildError("%s must be a string." % key)
        value = metadata.get(key, "")
        if value and not _is_https_url(value):
            raise BuildError("%s must be an https URL." % key)
    if any(char in metadata["maintainer_name"] for char in "<>\r\n"):
        raise BuildError("maintainer_name must not contain angle brackets or line breaks.")


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not any(
        char.isspace() for char in value
    )


def _validate_python(root: pathlib.Path = ROOT) -> None:
    for path in sorted((root / "src" / APP_ID).rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))
        except (SyntaxError, UnicodeError) as exc:
            raise BuildError("Python 3.10 validation failed for %s: %s" % (path, exc)) from exc


def _read_icon(root: pathlib.Path = ROOT) -> bytes:
    path = root / "packaging" / "images" / "icons" / "naspeed.svg"
    data = path.read_bytes()
    if len(data) > 50 * 1024:
        raise BuildError("Application icon exceeds the 50 KiB limit.")
    if re.search(br"<!\s*(DOCTYPE|ENTITY)\b", data, re.I):
        raise BuildError("Application icon must not declare a DOCTYPE or ENTITY.")
    try:
        tree = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise BuildError("Application icon is not valid XML: %s" % exc) from exc
    forbidden = {"script", "foreignobject", "iframe", "object", "embed"}
    count = 0
    for element in tree.iter():
        count += 1
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in forbidden:
            raise BuildError("Application icon contains a forbidden <%s> element." % tag)
        for name, value in element.attrib.items():
            if name.lower().startswith("on"):
                raise BuildError("Application icon contains an event-handler attribute.")
            if name.rsplit("}", 1)[-1].lower() in ("href", "src") and re.match(
                r"\s*(javascript|vbscript|file|ftp|about|blob):", value, re.I
            ):
                raise BuildError("Application icon contains an unsafe URL.")
    if count > 50:
        raise BuildError("Application icon exceeds the 50-element limit.")
    return data


def _validate_package_sources(root: pathlib.Path = ROOT) -> tuple[str, bool, bytes]:
    version, beta = _read_version(root)
    _validate_python(root)
    icon = _read_icon(root)
    webui = root / "webui"
    if icon != (webui / "favicon.svg").read_bytes():
        raise BuildError("Keep packaging/images/icons/naspeed.svg in sync with webui/favicon.svg.")
    required = ("index.html", "app.js", "i18n.js", "prefs.js", "styles.css", "favicon.svg")
    missing = [name for name in required if not (webui / name).is_file()]
    if missing:
        raise BuildError("Missing web UI files: %s." % ", ".join(missing))
    for name in SCRIPT_NAMES:
        path = root / "packaging" / "DEBIAN" / name
        if not path.is_file() or not path.read_bytes().startswith(b"#!/bin/bash\n"):
            raise BuildError("Missing or invalid lifecycle script: %s." % path)
    service = root / "packaging" / "init.d" / "naspeed.service"
    service_text = service.read_text(encoding="utf-8")
    for directive in (
        "User=naspeed", "Group=naspeed", "WorkingDirectory=/usr/local/naspeed",
        "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true",
        "ReadWritePaths=/usr/local/naspeed/data /usr/local/naspeed/logs /var/api",
        "TimeoutStartSec=30", "TimeoutStopSec=10", "StartLimitBurst=5",
        "StartLimitIntervalSec=60", "WantedBy=multi-user.target",
    ):
        if directive not in service_text:
            raise BuildError("Systemd unit is missing required directive: %s." % directive)
    if re.search(r"^Restart(?:Sec)?\s*=", service_text, re.M):
        raise BuildError("Do not set Restart or RestartSec; TOS manages service restarts.")
    return version, beta, icon


def _validate_version_progress(root: pathlib.Path, version: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "tag", "--list"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode:
        return
    numeric_tags = []
    for tag in result.stdout.splitlines():
        match = re.fullmatch(r"v?(\d+(?:\.\d+){0,2})", tag.strip())
        if match:
            numeric_tags.append(tuple(int(part) for part in match.group(1).split(".")))
    if not numeric_tags:
        return
    latest = max(tag + (0,) * (3 - len(tag)) for tag in numeric_tags)
    candidate_parts = tuple(int(part) for part in version.split("."))
    candidate = candidate_parts + (0,) * (3 - len(candidate_parts))
    if candidate <= latest:
        raise BuildError(
            "Version %s must be greater than the latest numeric git tag (%s)."
            % (version, ".".join(str(part) for part in latest))
        )


def _config(metadata: dict, version: str, beta: bool) -> bytes:
    config = {
        "id": APP_ID,
        "icon": "/images/icons/naspeed.svg",
        "publisher": metadata["publisher"].strip(),
        "exec": True,
        "type": "iframe",
        "path": "/naspeed/",
        "resize": True,
        "maxmin": True,
        "width": 1180,
        "height": 680,
        "help": metadata["help_url"].strip(),
        "version": version,
        "recommend": False,
        "beta": beta,
        "low_version": "7.0",
        "category": ["Utilities"],
        "relation": [],
        "platform": "x86_64",
        "application_type": "deb",
        "system_id": "naspeed",
        "package": "naspeed",
        "user": "naspeed",
        "allow_open_in_mobile": False,
    }
    if metadata.get("official_url", "").strip():
        config["official"] = metadata["official_url"].strip()
    for key, expected in REQUIRED_CONFIG.items():
        if key not in config or type(config[key]) is not expected:
            raise BuildError("Invalid config.ini field %s." % key)
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _quote_lang(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise BuildError("Language fields cannot contain line breaks.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _language_file(publisher: str, version: str) -> bytes:
    fields = {
        "name": "NASpeed",
        "auth": publisher.strip(),
        "descript": "Internet speed test with Cloudflare, Ookla, LibreSpeed and M-Lab.",
        "release_note": "%s beta build." % version,
        "important": "",
    }
    if any(not fields[key] for key in ("name", "auth", "descript")):
        raise BuildError("name, auth and descript must not be empty in every language.")
    lines = []
    for language in LANGUAGES:
        lines.append("[%s]" % language)
        lines.extend("%s = %s" % (key, _quote_lang(fields[key])) for key in LANGUAGE_FIELDS)
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def _control_file(metadata: dict, version: str) -> bytes:
    maintainer = "%s <%s>" % (
        metadata["maintainer_name"].strip(),
        metadata["maintainer_email"].strip(),
    )
    text = (
        "Package: naspeed\n"
        "Version: %s\n"
        "Architecture: amd64\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Maintainer: %s\n"
        "Depends: python3 (>= 3.10), systemd\n"
        "Description: %s\n"
        " Multi-provider internet speed test for TerraMaster TOS 7.\n"
    ) % (version, maintainer, DESCRIPTION)
    return text.encode("utf-8")


def _webui_archive(root: pathlib.Path, version: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:bz2", format=tarfile.USTAR_FORMAT) as archive:
        files = sorted(path for path in (root / "webui").iterdir() if path.is_file())
        for path in files:
            data = path.read_bytes()
            if path.suffix.lower() in (".html", ".js", ".css", ".svg", ".json"):
                data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if path.name == "index.html":
                data = data.replace(b"__VERSION__", version.encode("ascii"))
            info = tarfile.TarInfo(path.name)
            info.size = len(data)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _zipapp(root: pathlib.Path, destination: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory(prefix="naspeed-zipapp-") as temp:
        package_root = pathlib.Path(temp)
        shutil.copytree(
            root / "src" / APP_ID,
            package_root / APP_ID,
            ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
        )
        for path in (package_root / APP_ID).rglob("*.py"):
            data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            path.write_bytes(data)
        with (package_root / "__main__.py").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(ARCHIVE_ENTRY)
        zipapp.create_archive(
            str(package_root), str(destination), interpreter="/usr/bin/python3", compressed=True
        )
    destination.chmod(0o755)
    result = subprocess.run(
        [sys.executable, "-I", str(destination), "--version"],
        check=False, capture_output=True, text=True,
    )
    version, _beta = _read_version(root)
    if result.returncode or result.stdout.strip() != version:
        raise BuildError("The generated zipapp failed its --version smoke test: %s" % result.stderr)


def _tar_gz(paths: list[tuple[pathlib.Path, str]]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for source, name in sorted(paths, key=lambda item: item[1]):
                info = tarfile.TarInfo(name)
                if source.is_dir():
                    data = b""
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    name = name.rstrip("/") + "/"
                    info.name = name
                else:
                    data = source.read_bytes()
                    info.mode = 0o755 if name in SCRIPT_NAMES or name.endswith("/bin/naspeed") else 0o644
                info.size = len(data)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data) if data else None)
    return output.getvalue()


def _ar_member(name: str, payload: bytes) -> bytes:
    if len(name) > 15 or "/" in name:
        raise BuildError("Invalid ar member name: %s." % name)
    header = (
        (name + "/").ljust(16)
        + "0".ljust(12)
        + "0".ljust(6)
        + "0".ljust(6)
        + "100644".ljust(8)
        + str(len(payload)).ljust(10)
        + "`\n"
    )
    if len(header) != 60:
        raise BuildError("Internal error while writing ar member header.")
    return header.encode("ascii") + payload + (b"\n" if len(payload) % 2 else b"")


def _write_ar(destination: pathlib.Path, members: list[tuple[str, bytes]]) -> None:
    with destination.open("wb") as output:
        output.write(b"!<arch>\n")
        for name, payload in members:
            output.write(_ar_member(name, payload))


def build_package(
    root: pathlib.Path = ROOT,
    output_dir: pathlib.Path | None = None,
    metadata: dict | None = None,
) -> pathlib.Path:
    root = pathlib.Path(root).resolve()
    output_dir = pathlib.Path(output_dir or root / "dist").resolve()
    version, beta, icon = _validate_package_sources(root)
    _validate_version_progress(root, version)
    metadata = _load_metadata(root) if metadata is None else metadata
    _validate_metadata(metadata)

    with tempfile.TemporaryDirectory(prefix="naspeed-build-") as temp:
        staging = pathlib.Path(temp)
        payload_root = staging / "data" / "usr" / "local" / APP_ID
        control_root = staging / "control" / "DEBIAN"
        for directory in (
            payload_root / "bin",
            payload_root / "images" / "icons",
            payload_root / "init.d",
            control_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        executable = payload_root / "bin" / APP_ID
        _zipapp(root, executable)
        (payload_root / "config.ini").write_bytes(_config(metadata, version, beta))
        (payload_root / "naspeed.lang").write_bytes(
            _language_file(metadata["publisher"], version)
        )
        (payload_root / "images" / "icons" / "naspeed.svg").write_bytes(icon)
        shutil.copyfile(root / "packaging" / "init.d" / "naspeed.service",
                        payload_root / "init.d" / "naspeed.service")
        service_path = payload_root / "init.d" / "naspeed.service"
        service_path.write_bytes(
            service_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        )
        (payload_root / "webui.bz2").write_bytes(_webui_archive(root, version))
        shutil.copyfile(root / "LICENSE", payload_root / "LICENSE")

        (control_root / "control").write_bytes(_control_file(metadata, version))
        control_paths = []
        for script in SCRIPT_NAMES:
            source = root / "packaging" / "DEBIAN" / script
            destination = control_root / script
            destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            control_paths.append((destination, script))
        control_paths.append((control_root / "control", "control"))
        control_tar = _tar_gz(control_paths)
        data_paths = [
            (path, path.relative_to(staging / "data").as_posix())
            for path in (staging / "data").rglob("*")
        ]
        data_tar = _tar_gz(data_paths)
        for name in ("config.ini", "naspeed.lang", "images/icons/naspeed.svg", "webui.bz2"):
            packaged_file = payload_root / name
            if packaged_file.stat().st_size > 1024 * 1024:
                raise BuildError("%s exceeds the 1 MiB platform file limit." % name)
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / ("%s_x86_64.deb" % APP_ID)
        temporary_output = destination.with_suffix(".deb.tmp")
        try:
            _write_ar(temporary_output, [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", control_tar),
                ("data.tar.gz", data_tar),
            ])
            if temporary_output.stat().st_size > 2 * 1024 * 1024 * 1024:
                raise BuildError("The package exceeds the 2 GiB platform limit.")
            temporary_output.replace(destination)
        finally:
            if temporary_output.exists():
                temporary_output.unlink()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true",
                      help="validate package source files without developer metadata")
    mode.add_argument("--release-info", action="store_true",
                      help="print the version and beta flag as key=value lines, "
                           "ready to append to $GITHUB_OUTPUT")
    parser.add_argument("--output-dir", type=pathlib.Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        if args.release_info:
            version, beta = _read_version(ROOT)
            print("version=%s" % version)
            print("beta=%s" % ("true" if beta else "false"))
            return 0
        version, beta, _icon = _validate_package_sources(ROOT)
        if args.validate_only:
            print("NASpeed %s (%s): package sources validated." %
                  (version, "beta" if beta else "stable"))
            return 0
        destination = build_package(ROOT, args.output_dir)
    except BuildError as exc:
        print("Build error: %s" % exc, file=sys.stderr)
        return 2
    print("Built %s" % destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
