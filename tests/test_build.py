# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Offline tests for the TerraMaster .deb build pipeline."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

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


def build_deb(output_dir):
    # Published versions are tagged and build_package refuses a version that does
    # not exceed the latest tag. These tests check the package contents, so they
    # must not depend on the tags of the clone they run in.
    with mock.patch.object(build, "_validate_version_progress"):
        return build.build_package(ROOT, pathlib.Path(output_dir), TEST_METADATA)


def read_ar(path):
    data = pathlib.Path(path).read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise AssertionError("Not an ar archive")
    offset = 8
    members = {}
    while offset < len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode("ascii").strip()
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
        content = build._language_file("NASpeed test publisher", "Faster upgrades.").decode("utf-8")
        sections = [line[1:-1] for line in content.splitlines() if line.startswith("[")]
        self.assertEqual(sections, list(build.LANGUAGES))
        self.assertEqual(sections[:14], list(build.REQUIRED_LANGUAGES))
        descriptions = set()
        for section in content.split("\n\n"):
            values = dict(
                line.split(" = ", 1) for line in section.splitlines()[1:] if " = " in line
            )
            for key in ("name", "auth", "descript"):
                self.assertIn(key, values)
                self.assertNotEqual(values[key], '""')
            for provider in ("Cloudflare", "Ookla", "LibreSpeed", "M-Lab"):
                self.assertIn(provider, values["descript"])
            self.assertEqual(values["release_note"], '"Faster upgrades."')
            descriptions.add(values["descript"])
        self.assertEqual(len(descriptions), len(build.LANGUAGES))

    def test_release_note_is_the_summary_line_of_the_version_in_the_changelog(self):
        self.assertTrue(build._release_note(ROOT, __version__))
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                "# Changelog\n\n## 1.1.0\n\n- Only a list.\n\n## 1.0.0\n\nFirst release.\n\n- More.\n",
                encoding="utf-8",
            )
            self.assertEqual(build._release_note(root, "1.0.0"), "First release.")
            for version in ("1.1.0", "1.2.0"):
                with self.assertRaisesRegex(build.BuildError, "## %s" % version):
                    build._release_note(root, version)

    def test_package_is_a_valid_single_package_deb_with_expected_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = build_deb(temp)
            members = read_ar(destination)
            self.assertEqual(list(members), ["debian-binary", "control.tar.gz", "data.tar.gz"])
            self.assertEqual(members["debian-binary"], b"2.0\n")
            with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as archive:
                self.assertEqual(archive.getnames()[0], ".")
                self.assertEqual(len(archive.getnames()), 6)
                control = archive.extractfile("./control").read().decode("utf-8")
                self.assertIn("Version: %s\n" % __version__, control)
                self.assertIn("Maintainer: NASpeed test maintainer <builder@example.invalid>", control)
                self.assertTrue(
                    all("./" + script in archive.getnames() for script in build.SCRIPT_NAMES)
                )
            with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as archive:
                names = archive.getnames()
                self.assertEqual(names[0], ".")
                self.assertTrue(all(name == "." or name.startswith("./") for name in names))
                prefix = "./usr/local/naspeed/"
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
                    startup_script = b"startup.js?v=" + __version__.encode("ascii")
                    app_script = b"app.js?v=" + __version__.encode("ascii")
                    self.assertLess(index.index(startup_script), index.index(app_script))
                    startup = webui.extractfile("startup.js").read()
                    self.assertIn(b"function refreshForVersion", startup)
                executable = pathlib.Path(temp) / "naspeed"
                executable.write_bytes(archive.extractfile(prefix + "bin/naspeed").read())
                result = subprocess.run(
                    [sys.executable, "-I", str(executable), "--version"],
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual((result.returncode, result.stdout.strip()), (0, __version__))

    def test_package_archives_have_root_owned_safe_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = build_deb(temp)
            members = read_ar(destination)
            with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as archive:
                exe = archive.getmember("./usr/local/naspeed/bin/naspeed")
                icon = archive.getmember("./usr/local/naspeed/images/icons/naspeed.svg")
                app_dir = archive.getmember("./usr/local/naspeed/")
                self.assertEqual((exe.uid, exe.gid, exe.mode), (0, 0, 0o755))
                self.assertEqual((icon.uid, icon.gid, icon.mode), (0, 0, 0o644))
                self.assertEqual((app_dir.uid, app_dir.gid, app_dir.mode), (0, 0, 0o755))
            with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as archive:
                for script in build.SCRIPT_NAMES:
                    member = archive.getmember("./" + script)
                    self.assertEqual((member.uid, member.gid, member.mode), (0, 0, 0o755))

    def test_building_the_same_sources_twice_gives_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second, \
                mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            destination = build_deb(first)
            package = destination.read_bytes()
            self.assertEqual(build_deb(second).read_bytes(), package)
            members = read_ar(destination)
        self.assertEqual(package[8 + 16:8 + 28].strip(), b"1700000000")
        with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as archive:
            self.assertEqual({member.mtime for member in archive.getmembers()}, {1700000000})
            executable = archive.extractfile("./usr/local/naspeed/bin/naspeed").read()
            webui = archive.extractfile("./usr/local/naspeed/webui.bz2").read()
        with tarfile.open(fileobj=io.BytesIO(webui), mode="r:bz2") as archive:
            self.assertEqual({member.mtime for member in archive.getmembers()}, {1700000000})
        with zipfile.ZipFile(io.BytesIO(executable)) as archive:
            entries = archive.infolist()
        self.assertEqual({entry.date_time for entry in entries}, {(2023, 11, 14, 22, 13, 20)})
        self.assertEqual({entry.create_system for entry in entries}, {3})
        self.assertEqual({entry.external_attr >> 16 for entry in entries if not entry.is_dir()},
                         {0o100644})
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "yesterday"}):
            with self.assertRaisesRegex(build.BuildError, "SOURCE_DATE_EPOCH"):
                build._build_timestamp(ROOT)

    @unittest.skipUnless(
        os.name == "posix" and all(shutil.which(tool) for tool in ("bash", "tar", "bzip2")),
        "the install script needs bash, tar and bzip2",
    )
    def test_postinst_refreshes_the_web_interface_only_on_upgrade(self):
        script = (ROOT / "packaging" / "DEBIAN" / "postinst").read_text(encoding="utf-8")
        archive = build._webui_archive(ROOT, "9.9.9", 0)
        with tempfile.TemporaryDirectory() as temp:
            temp = pathlib.Path(temp)
            stubs = temp / "stubs"
            stubs.mkdir()
            (stubs / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (stubs / "systemctl").chmod(0o755)
            env = dict(os.environ, PATH=str(stubs) + os.pathsep + os.environ["PATH"])

            def run_postinst(app_dir):
                patched = temp / "postinst"
                patched.write_text(script.replace("/usr/local/naspeed", str(app_dir)), encoding="utf-8")
                return subprocess.run(["bash", str(patched), "configure"], env=env,
                                      capture_output=True, text=True, check=False)

            upgrade = temp / "upgrade"
            (upgrade / "webui").mkdir(parents=True)
            (upgrade / "webui" / "index.html").write_bytes(b"old")
            (upgrade / "webui" / "dropped.js").write_bytes(b"from an older version")
            (upgrade / "webui.bz2").write_bytes(archive)
            self.assertEqual(run_postinst(upgrade).returncode, 0)
            index = upgrade / "webui" / "index.html"
            self.assertIn(b"9.9.9", index.read_bytes())
            self.assertGreater(index.stat().st_mtime, 86400)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:bz2") as webui:
                shipped = sorted(webui.getnames())
            self.assertEqual(sorted(path.name for path in (upgrade / "webui").iterdir()), shipped)
            self.assertEqual(sorted(path.name for path in upgrade.iterdir()), ["webui", "webui.bz2"])

            fresh = temp / "fresh"
            fresh.mkdir()
            (fresh / "webui.bz2").write_bytes(archive)
            self.assertEqual(run_postinst(fresh).returncode, 0)
            self.assertFalse((fresh / "webui").exists())

            broken = temp / "broken"
            (broken / "webui").mkdir(parents=True)
            (broken / "webui" / "index.html").write_bytes(b"old")
            (broken / "webui" / "app.js").write_bytes(b"old")
            (broken / "webui.bz2").write_bytes(b"not a bzip2 archive")
            self.assertEqual(run_postinst(broken).returncode, 0)
            self.assertEqual(sorted(path.name for path in (broken / "webui").iterdir()),
                             ["app.js", "index.html"])
            self.assertEqual((broken / "webui" / "index.html").read_bytes(), b"old")
            self.assertEqual(sorted(path.name for path in broken.iterdir()), ["webui", "webui.bz2"])

    def test_source_validation_passes_without_developer_metadata(self):
        version, beta, icon = build._validate_package_sources(ROOT)
        self.assertEqual((version, beta), (__version__, BETA))
        self.assertEqual(icon, (ROOT / "webui" / "favicon.svg").read_bytes())

    def test_release_info_prints_the_version_and_beta_flag_as_github_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(build.main(["--release-info"]), 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            ["version=%s" % __version__, "beta=%s" % str(BETA).lower()],
        )


if __name__ == "__main__":
    unittest.main()
