"""One-off sanity check against the real vault. Not part of the test suite."""

import sys

sys.path.insert(0, "src")

from assistant.obsidian import ObsidianBridge

b = ObsidianBridge()
print(b.describe())
print()

s = b.index_stats()
for d, v in s["per_dir"].items():
    if "error" in v:
        print("%18s  ERROR %s" % (d, v["error"][:60]))
    else:
        print("%18s  %5d files  %8.2f MiB" % (d, v["files"], v["bytes"] / 1048576))
print("%18s  %5d files" % ("TOTAL", s["total_files"]))
print()

print("exclusion is enforced:")
for probe in ("datasets", ".smart-env", "somewhere_else", "Assistant"):
    try:
        b._read_path(probe, "x.md")
        print("  FAIL: %s was allowed" % probe)
    except Exception as exc:
        print("  refused %-15s %s" % (probe, type(exc).__name__))

print()
print("real notes the index can see (newest 5):")
for n in b.index()[:5]:
    print("  %s  %6d B" % (n.rel, n.size))
