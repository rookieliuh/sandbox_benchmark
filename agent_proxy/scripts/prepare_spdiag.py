#!/usr/bin/env python3
"""Unpack the reviewed RPM pair inside this project; never install RPMs or run scripts."""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = "1.0.0-1.p99_on.perflog_on.mempoint_on.obmem_on.obcache_on.aarch64.rpm"
PACKAGES = {
    "spdiag-" + SUFFIX: "dcf187a4419da3af3f03a88b8251246766ce52e9955a136ddb58be6bb6ad4580",
    "spdiag-devel-" + SUFFIX: "bca860f9ddac76a3f39708d6ce38bac233ee6b38578e9e16a68bf104267463fc",
}


def header_end(blob, offset):
    if blob[offset:offset + 3] != b"\x8e\xad\xe8":
        raise ValueError("invalid RPM header")
    count, size = struct.unpack_from(">II", blob, offset + 8)
    end = offset + 16 + count * 16 + size
    if end > len(blob):
        raise ValueError("truncated RPM header")
    return end


def rpm_entries(blob):
    """The pinned packages have an XZ-compressed newc payload. No shell/external extractor."""
    sig_end = header_end(blob, 96)
    payload = header_end(blob, (sig_end + 7) & ~7)
    data = lzma.decompress(blob[payload:])
    offset = 0
    while offset < len(data):
        if data[offset:offset + 6] != b"070701":
            raise ValueError("unexpected CPIO format")
        fields = [int(data[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        start = offset + 110
        name = data[start:start + name_size - 1].decode("utf-8")
        body = (start + name_size + 3) & ~3
        content = data[body:body + size]
        if len(content) != size:
            raise ValueError("truncated CPIO entry")
        offset = (body + size + 3) & ~3
        if name == "TRAILER!!!":
            return
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in ("usr", "etc"):
            raise ValueError(f"unsafe archive path: {name}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise ValueError(f"unsupported archive entry: {name}")
        yield path, mode, content
    raise ValueError("missing CPIO trailer")


def extract(entries, destination):
    links = []
    for path, mode, content in entries:
        target = destination.joinpath(*path.parts)
        if stat.S_ISDIR(mode):
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISLNK(mode):
            link = content.decode("utf-8")
            # This RPM pair contains only same-directory library links. Materialize them
            # as copies so preparation can also be checked on Windows without privileges.
            if PurePosixPath(link).name != link or link in ("", ".", ".."):
                raise ValueError(f"unsafe library link: {path}")
            links.append((target, target.parent / link))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
            target.chmod(mode & 0o777)
    while links:
        pending = []
        for target, source in links:
            if source.is_file():
                if target.exists():
                    raise ValueError(f"duplicate library entry: {target}")
                shutil.copyfile(source, target)
            else:
                pending.append((target, source))
        if len(pending) == len(links):
            raise ValueError("unresolved library link")
        links = pending


def relocate(destination):
    package_dir = destination / "usr/lib64/cmake/SpDiag"
    targets = package_dir / "SpDiagTargets.cmake"
    text = targets.read_text(encoding="utf-8")
    old = 'set(_IMPORT_PREFIX "/usr")'
    if text.count(old) != 1:
        raise ValueError("unexpected SpDiag CMake export; refusing an unreviewed layout")
    text = text.replace(old, 'get_filename_component(_IMPORT_PREFIX "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)')
    targets.write_text(text, encoding="utf-8", newline="\n")
    config = package_dir / "SpDiagTargets-relwithdebinfo.cmake"
    text = config.read_text(encoding="utf-8")
    if text.count('"/usr/lib64/libspdiag.so.1.0.0"') != 2:
        raise ValueError("unexpected SpDiag library export")
    config.write_text(text.replace('"/usr/lib64/', '"${_IMPORT_PREFIX}/lib64/'),
                      encoding="utf-8", newline="\n")
    # The devel RPM also ships static exports, but no static libraries; shared exports
    # are selected by SpDiagConfig. Leave the unused static exports untouched.


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(rpm_dir):
    blobs = []
    for name, expected in PACKAGES.items():
        path = rpm_dir / name
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError(f"RPM SHA256 differs from reviewed package: {name}")
        blobs.append(blob)
    deps = ROOT / ".deps"
    if not deps.resolve().is_relative_to(ROOT):
        raise ValueError(".deps must stay inside agent_proxy")
    deps.mkdir(exist_ok=True)
    destination = deps / "spdiag"
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise ValueError("refusing a symlink destination")
        manifest = json.loads((destination / "agent-proxy-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("packages") != PACKAGES:
            raise ValueError("existing SpDiag directory has different packages")
        for name, expected in manifest["files"].items():
            path = destination / name
            if not path.resolve().is_relative_to(destination) or digest(path) != expected:
                raise ValueError(f"existing dependency changed: {name}; preserve it and use a fresh checkout")
        print(f"Verified existing dependency: {destination}")
        return
    # A new staging directory prevents partial installation from looking complete.
    stage = Path(tempfile.mkdtemp(prefix="spdiag-stage-", dir=deps))
    try:
        entries = [entry for blob in blobs for entry in rpm_entries(blob)]
        extract(entries, stage)
        relocate(stage)
        files = {p.relative_to(stage).as_posix(): digest(p) for p in stage.rglob("*") if p.is_file()}
        (stage / "agent-proxy-manifest.json").write_text(
            json.dumps({"packages": PACKAGES, "files": files}, indent=2), encoding="utf-8")
        stage.rename(destination)
    except Exception:
        # Preserve partial output for diagnosis; no recursive deletion of user paths.
        print(f"Incomplete preparation retained at: {stage}")
        raise
    print(f"Prepared: {destination}")
    print("Only project files were written. No RPM scripts, system install, or ldconfig ran.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpm-dir", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.rpm_dir.resolve())


if __name__ == "__main__":
    main()
