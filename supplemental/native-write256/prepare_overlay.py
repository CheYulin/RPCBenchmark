#!/usr/bin/env python3
"""Extract the verified pin into a private include tree, then apply one opt-in patch."""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import tarfile

PIN = '45c6463c0afbcc46262e373effa30635be3846a0'
ARCHIVE_SHA256 = 'baa6b5c9ffcc0af8252742bba8f0fb9c6d37387c13b2ff5b26d29c6aaff6ee37'
HEADER_SHA256 = 'b34f131a902366cc0047e12356da339496d01f99c8a153907469be606367fa2f'
HEADER = pathlib.Path('include/ylt/coro_io/coro_io.hpp')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(archive, output):
    archive, output = archive.resolve(), output.resolve()
    if sha(archive) != ARCHIVE_SHA256:
        raise ValueError('archive does not match the pinned SHA256')
    if output.exists():
        raise FileExistsError(output)
    patch = pathlib.Path(__file__).with_name('coro-write256.patch')
    output.mkdir(parents=True)
    files, total = 0, 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            parts = pathlib.PurePosixPath(member.name).parts
            if not parts or parts[0] != 'yalantinglibs-' + PIN:
                raise ValueError('unexpected archive root')
            relative = pathlib.PurePosixPath(*parts[1:])
            if not relative.parts or relative.parts[0] != 'include':
                continue
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('unexpected archive path')
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('include tree must contain only regular files')
            total += member.size
            if total > 256 * 1024 * 1024:
                raise ValueError('include tree exceeds extraction budget')
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            files += 1
    header = output / HEADER
    if sha(header) != HEADER_SHA256:
        raise ValueError('unexpected pinned wrapper content')
    subprocess.run(['patch', '-p1', '--fuzz=0', '--no-backup-if-mismatch',
                    '--directory', str(output), '--input', str(patch.resolve())], check=True)
    manifest = {'pin': PIN, 'archive_sha256': ARCHIVE_SHA256, 'include_files': files,
                'include_bytes': total, 'original_header_sha256': HEADER_SHA256,
                'patched_header_sha256': sha(header), 'patch_sha256': sha(patch),
                'macro': 'RPC_LAB_CORO_WRITE_256K', 'enabled_limit_bytes': 262144,
                'default_limit_bytes': 65536}
    (output / 'overlay-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True, type=pathlib.Path)
    parser.add_argument('--output', required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.output), indent=2))
