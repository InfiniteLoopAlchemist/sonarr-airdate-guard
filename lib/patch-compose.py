#!/usr/bin/env python3
"""
patch-compose.py <compose-file> <container-name> <host-guard-dir>

Adds a read-write bind mount of the guard directory at /guard to one service in
a docker-compose file, in place, preserving comments and formatting.

Deliberately line-based rather than YAML round-tripping. Loading and re-dumping
with PyYAML would strip every comment and reflow the file, which is a rude thing
to do to somebody's infrastructure.

Exit codes:
    0  patched
    3  already mounts /guard, nothing to do
    4  could not find the service, or its volumes are written inline
"""

import os
import re
import sys

ALREADY = 3
UNSUPPORTED = 4


def indent_of(line):
    return len(line) - len(line.lstrip())


def find_service_block(lines, container):
    """Return (start, end) line indices of the service whose container_name is
    `container`, falling back to a service key of the same name.

    Compose services live two levels in:

        services:
          sonarr:              <- service key, this is what we bracket
            container_name: sonarr
            volumes:
              - ./config:/config
    """
    services_start = None
    services_indent = 0
    for i, line in enumerate(lines):
        if re.match(r"^\s*services:\s*$", line):
            services_start = i
            services_indent = indent_of(line)
            break
    if services_start is None:
        return None

    # Bracket every service key under services:
    blocks = []
    key_indent = None
    current = None
    for i in range(services_start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = indent_of(line)
        if ind <= services_indent:
            break  # left the services: mapping entirely
        if key_indent is None:
            key_indent = ind
        if ind == key_indent and re.match(r"^\s*[\w.-]+:\s*$", line):
            if current is not None:
                blocks.append((current[0], current[1], i))
            current = (line.strip().rstrip(":"), i)
    if current is not None:
        blocks.append((current[0], current[1], len(lines)))

    # Prefer an explicit container_name match.
    for name, start, end in blocks:
        for i in range(start, end):
            m = re.match(r"^\s*container_name:\s*[\"']?([\w.-]+)[\"']?\s*$", lines[i])
            if m and m.group(1) == container:
                return start, end

    for name, start, end in blocks:
        if name == container:
            return start, end

    return None


def patch(compose_path, container, guard_dir):
    with open(compose_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    if any(re.search(r":/guard(\s|$|['\"])", ln) for ln in lines):
        return ALREADY

    block = find_service_block(lines, container)
    if block is None:
        sys.stderr.write(f"no service for container '{container}' in {compose_path}\n")
        return UNSUPPORTED
    start, end = block

    mount = f"{guard_dir}:/guard"

    # Find this service's volumes: key.
    service_indent = indent_of(lines[start])
    volumes_at = None
    for i in range(start + 1, end):
        line = lines[i]
        if not line.strip():
            continue
        if indent_of(line) <= service_indent:
            break
        if re.match(r"^\s*volumes:\s*$", line):
            volumes_at = i
            break
        if re.match(r"^\s*volumes:\s*\[", line):
            sys.stderr.write(
                f"{compose_path}: '{container}' writes volumes inline as a flow "
                f"sequence. Add this entry by hand:  {mount}\n"
            )
            return UNSUPPORTED

    if volumes_at is None:
        # No volumes at all. Add the whole key right after the service header.
        item_indent = " " * (service_indent + 2)
        lines.insert(start + 1, f"{item_indent}volumes:\n")
        lines.insert(start + 2, f"{item_indent}  - {mount}\n")
    else:
        # Match the indentation of the first existing list item.
        item_indent = " " * (indent_of(lines[volumes_at]) + 2)
        for i in range(volumes_at + 1, end):
            if lines[i].strip().startswith("- "):
                item_indent = " " * indent_of(lines[i])
                break
        lines.insert(volumes_at + 1, f"{item_indent}- {mount}\n")

    tmp = compose_path + ".guardtmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp, compose_path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.stderr.write(__doc__)
        sys.exit(2)
    sys.exit(patch(sys.argv[1], sys.argv[2], sys.argv[3]))
