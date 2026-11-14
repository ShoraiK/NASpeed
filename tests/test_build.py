# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Offline tests for the TerraMaster .deb build pipeline."""

import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from naspeed import BETA, __version__  # noqa: E402

SPEC = importlib.util.spec_from_file_location("naspeed_build", ROOT / "tools" / "build.py")
build = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build
SPEC.loader.exec_module(build)

TEST_METADATA = {
    "publisher": "NASpeed test publisher",
    "maintainer_name": "NASpeed test maintainer",
    "maintainer_email": "builder@example.invalid",
    "help_url": "https://example.invalid/naspeed",
    "official_url": "",
}


def read_ar(path):
    data = pathlib.Path(path).read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise AssertionError("Not an ar archive")
    offset = 8
    members = {}
    while offset < len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        offset += 60
        members[name] = data[offset:offset + size]
        offset += size + (size % 2)
    return members


class PackageBuildTests(unittest.TestCase):
    def test_version_is_read_and_restricted_to_numeric_segments(self):
        self.assertEqual(build._read_version(ROOT), (__version__, BETA))
        with tempfile.TemporaryDirectory() as temp:
            temp_root = pathlib.Path(temp)
            init = temp_root / "src" / "naspeed" / "__init__.py"
            init.parent.mkdir(parents=True)
            init.write_text('__version__ = "0.1.0-beta"\nBETA = True\n', encoding="utf-8")
            with self.assertRaisesRegex(build.BuildError, "numeric segments"):
                build._read_version(temp_root)

    def test_missing_contact_metadata_prevents_build_without_leaking_defaults(self):
        metadata = dict(TEST_METADATA, maintainer_email="")
        with self.assertRaisesRegex(build.BuildError, "maintainer_email"):
            build._validate_metadata(metadata)

    def test_version_must_exceed_the_latest_numeric_release_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=Test",
                 "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                 "-m", "initial"],
                check=True, capture_output=True,
            )
            subprocess.run(["git", "-C", str(root), "tag", "v0.1"], check=True)
            with self.assertRaisesRegex(build.BuildError, "must be greater"):
                build._validate_version_progress(root, "0.1.0")
            build._validate_version_progress(root, "0.1.1")

    def test_language_file_has_all_platform_sections_and_required_fields(self):
        content = build._language_file("NASpeed test publisher", "0.1.0").decode("utf-8")
        sections = [line[1:-1] for line in content.splitlines() if line.startswith("[")]
        self.assertEqual(sections, list(build.LANGUAGES))
        self.assertEqual(sections[:14], list(build.REQUIRED_LANGUAGES))
        for section in content.split("\n\n"):
            values = dict(
                line.split(" = ", 1) for line in section.splitlines()[1:] if " = " in line
            )
            for key in ("name", "auth", "descript"):
                self.assertIn(key, values)
                self.assertNotEqual(values[key], '""')

    def test_package_is_a_valid_single_package_deb_with_expected_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = build.build_package(ROOT, pathlib.Path(temp), TEST_METADATA)
            members = read_ar(destination)
            self.assertEqual(list(members), ["debian-binary", "control.tar.gz", "data.tar.gz"])
            self.assertEqual(members["debian-binary"], b"2.0\n")
            with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as archive:
                self.assertEqual(len(archive.getnames()), 5)
                control = archive.extractfile("control").read().decode("utf-8")
                self.assertIn("Version: %s\n" % __version__, control)
                self.assertIn("Maintainer: NASpeed test maintainer <builder@example.invalid>", control)
                self.assertTrue(all(script in archive.getnames() for script in build.SCRIPT_NAMES))
            with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as archive:
                names = archive.getnames()
                prefix = "usr/local/naspeed/"
                self.assertIn(prefix + "config.ini", names)
                self.assertIn(prefix + "naspeed.lang", names)
                self.assertIn(prefix + "LICENSE", names)
                self.assertIn(prefix + "init.d/naspeed.service", names)
                self.assertIn(prefix + "images/icons/naspeed.svg", names)
                config = json.load(archive.extractfile(prefix + "config.ini"))
                self.assertEqual(config["version"], __version__)
                self.assertIs(config["beta"], BETA)
                self.assertEqual(config["low_version"], "7.0")
                with tarfile.open(fileobj=io.BytesIO(archive.extractfile(prefix + "webui.bz2").read()),
                                  mode="r:bz2") as webui:
                    index = webui.extractfile("index.html").read()
                    self.assertNotIn(b"__VERSION__", index)
                    self.assertIn(__version__.encode("ascii"), index)
                executable = pathlib.Path(temp) / "naspeed"
                executable.write_bytes(archive.extractfile(prefix + "bin/naspeed").read())
                result = subprocess.run(
                    [sys.executable, "-I", str(executable), "--version"],
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual((result.returncode, result.stdout.strip()), (0, __version__))

    def test_package_archives_have_root_owned_safe_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = build.build_package(ROOT, pathlib.Path(temp), TEST_METADATA)
            members = read_ar(destination)
            with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as archive:
                exe = archive.getmember("usr/local/naspeed/bin/naspeed")
                icon = archive.getmember("usr/local/naspeed/images/icons/naspeed.svg")
                app_dir = archive.getmember("usr/local/naspeed/")
                self.assertEqual((exe.uid, exe.gid, exe.mode), (0, 0, 0o755))
                self.assertEqual((icon.uid, icon.gid, icon.mode), (0, 0, 0o644))
                self.assertEqual((app_dir.uid, app_dir.gid, app_dir.mode), (0, 0, 0o755))
            with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as archive:
                for script in build.SCRIPT_NAMES:
                    member = archive.getmember(script)
                    self.assertEqual((member.uid, member.gid, member.mode), (0, 0, 0o755))

    def test_source_validation_passes_without_developer_metadata(self):
        version, beta, icon = build._validate_package_sources(ROOT)
        self.assertEqual((version, beta), (__version__, BETA))
        self.assertEqual(icon, (ROOT / "webui" / "favicon.svg").read_bytes())


if __name__ == "__main__":
    unittest.main()
