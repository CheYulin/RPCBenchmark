#!/usr/bin/env python3
"""Generate two private diagnostic cores from one exact, external source file."""
from pathlib import Path
import argparse
import hashlib
import json

ORIGINAL_SHA256 = '23bb005761a9012c994a532f04fc2c7c60b446737b003c86ce63999936437566'
BASE_SHA256 = '0ecf646a7256826ef66cf991b19fa99d7f7b9d64b2bb53f1f1d3739065052401'
INLINE_SHA256 = 'f73c0b18fefd6f1836bd2afec4a22a11a0e74df1f3d3d4b4087fd2d798caa024'
BRANCH = '''            if (request.method == "diag.echo") {
                g_diagInlineEchoCalls.fetch_add(1, std::memory_order_relaxed);
                Respond(std::move(context), Run(std::move(request)));
                Finish();
                return;
            }
'''
GETTER = '''extern "C" uint64_t DSCoroDiagInlineEchoCount() noexcept
{
    return g_diagInlineEchoCalls.load(std::memory_order_relaxed);
}

'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def variants(data):
    if digest(data) != ORIGINAL_SHA256:
        raise ValueError('DataSystem core source SHA256 mismatch')
    original = data.decode()
    atomic = 'std::atomic<size_t> g_serverAttachmentBytes{ 0 };\n'
    now = 'int64_t NowUs()\n{'
    background = '            if (StartBackgroundTask(nullptr, [this, context, request = std::move(request)]() mutable {\n'
    if any(original.count(anchor) != 1 for anchor in (atomic, now, background)):
        raise ValueError('DataSystem core anchors do not match')
    base = original.replace(atomic, atomic + 'std::atomic<uint64_t> g_diagInlineEchoCalls{ 0 };\n')
    base = base.replace(now, GETTER + now)
    inline = base.replace(background, BRANCH + background)
    result = {'base-private-core.cpp': base.encode(), 'inline-private-core.cpp': inline.encode()}
    if digest(result['base-private-core.cpp']) != BASE_SHA256 or digest(result['inline-private-core.cpp']) != INLINE_SHA256:
        raise ValueError('generated core source SHA256 mismatch')
    return result


def write_variants(source, output):
    generated = variants(Path(source).read_bytes())
    output = Path(output)
    if not output.is_dir() or any((output / name).exists() for name in generated):
        raise ValueError('output must exist and generated paths must be new')
    for name, data in generated.items():
        with (output / name).open('xb') as stream:
            stream.write(data)
    return {name: digest(data) for name, data in generated.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='existing staging directory')
    args = parser.parse_args()
    try:
        print(json.dumps(write_variants(args.source, args.output)))
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
