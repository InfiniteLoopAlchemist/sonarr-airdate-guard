#!/usr/bin/env bash
# install.sh - set up the Sonarr air-date guard on a Docker host.
#
# Idempotent. Safe to re-run after editing any of the scripts; re-running is how
# you upgrade.
#
#   1. finds every running Sonarr container
#   2. writes the scripts and one shared config into the guard directory
#   3. generates a config section per instance, reading each one's port and key
#      out of its own /config/config.xml
#   4. bind-mounts the guard directory into every Sonarr container at /guard
#   5. recreates only the containers whose compose file actually changed
#   6. installs the cron entries
#   7. creates the Custom Script connection on each instance over the API
#
# It never overwrites an existing config. There is nothing to click in the
# Sonarr UI afterwards.
#
# Environment:
#   SONARR_GUARD_DIR   where to install (default: reuse an existing /guard mount,
#                      else $HOME/sonarr-guard)
#   SONARR_GUARD_CRON  set to 0 to skip installing cron entries
#   DRY_RUN            set to 1 to print what would happen and change nothing

set -euo pipefail

STAGE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN="${DRY_RUN:-0}"

step()  { printf '\n==> %s\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
warn()  { printf '    WARNING: %s\n' "$*" >&2; }
fail()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
run()   { if [ "$DRY_RUN" = "1" ]; then printf '    would run: %s\n' "$*"; else "$@"; fi; }

command -v docker  >/dev/null || fail "docker is not on PATH"
command -v python3 >/dev/null || fail "python3 is required on the host (the containers do not need it)"
docker info >/dev/null 2>&1   || fail "cannot talk to the Docker daemon. Are you in the docker group?"

# --- find the Sonarr containers -------------------------------------------

step "looking for Sonarr containers"

CONTAINERS=()
while IFS= read -r name; do
    [ -n "$name" ] && CONTAINERS+=("$name")
done < <(docker ps --format '{{.Names}}\t{{.Image}}' \
         | awk -F'\t' 'tolower($2) ~ /sonarr/ { print $1 }')

[ ${#CONTAINERS[@]} -gt 0 ] || fail "no running containers with 'sonarr' in the image name"

for c in "${CONTAINERS[@]}"; do
    info "found $c"
done

# --- where does everything live -------------------------------------------

# If a container already mounts something at /guard, reuse that host path so a
# re-run upgrades the existing install instead of creating a second one.
EXISTING_GUARD=""
for c in "${CONTAINERS[@]}"; do
    src=$(docker inspect "$c" \
          --format '{{range .Mounts}}{{if eq .Destination "/guard"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)
    if [ -n "$src" ]; then
        EXISTING_GUARD="$src"
        break
    fi
done

GUARD_DIR="${SONARR_GUARD_DIR:-${EXISTING_GUARD:-$HOME/sonarr-guard}}"

step "guard directory: $GUARD_DIR"
[ -n "$EXISTING_GUARD" ] && [ -z "${SONARR_GUARD_DIR:-}" ] && info "reusing the existing /guard mount"

run mkdir -p "$GUARD_DIR"

for f in sonarr_guard_common.py sonarr-airdate-guard.py sonarr-grab-hook.sh; do
    [ -f "$STAGE/$f" ] || fail "$f is missing from $STAGE"
    run install -m 755 "$STAGE/$f" "$GUARD_DIR/$f"
    info "installed $f"
done
run mkdir -p "$GUARD_DIR/lib"
run install -m 755 "$STAGE/lib/patch-compose.py" "$GUARD_DIR/lib/patch-compose.py"
run install -m 755 "$STAGE/lib/setup-connections.py" "$GUARD_DIR/lib/setup-connections.py"

run touch "$GUARD_DIR/sonarr-guard.log"
run chmod 664 "$GUARD_DIR/sonarr-guard.log"

# --- generate the config ---------------------------------------------------

CONF="$GUARD_DIR/sonarr-guard.conf"

# container name -> config section name
section_for() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g'
}

if [ -f "$CONF" ]; then
    step "config already exists, left untouched"
    info "$CONF"
    info "delete it and re-run if you want it regenerated"
else
    step "generating $CONF"

    tmp="$(mktemp)"
    cat "$STAGE/sonarr-guard.conf.example" > "$tmp"
    printf '\n' >> "$tmp"

    for c in "${CONTAINERS[@]}"; do
        section="$(section_for "$c")"

        xml="$(docker exec "$c" cat /config/config.xml 2>/dev/null || true)"
        if [ -z "$xml" ]; then
            warn "$c: could not read /config/config.xml, skipping"
            continue
        fi

        cport=$(printf '%s' "$xml" | sed -n 's:.*<Port>\(.*\)</Port>.*:\1:p' | head -1)
        ckey=$(printf  '%s' "$xml" | sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' | head -1)
        cbase=$(printf '%s' "$xml" | sed -n 's:.*<UrlBase>\(.*\)</UrlBase>.*:\1:p' | head -1)

        if [ -z "$cport" ] || [ -z "$ckey" ]; then
            warn "$c: no port or key in config.xml, skipping"
            continue
        fi

        cbase="${cbase#/}"
        [ -n "$cbase" ] && cbase="/$cbase"

        # The poller runs on the host, so prefer the published port. Fall back to
        # the container's address on its Docker network.
        mapped=$(docker port "$c" "${cport}/tcp" 2>/dev/null | head -1 || true)
        if [ -n "$mapped" ]; then
            url="http://127.0.0.1:${mapped##*:}${cbase}"
        else
            ip=$(docker inspect "$c" \
                 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $1}')
            [ -n "$ip" ] || { warn "$c: no published port and no container IP, skipping"; continue; }
            url="http://${ip}:${cport}${cbase}"
            warn "$c: port $cport is not published, using the container address $ip"
        fi

        {
            printf '# container: %s\n' "$c"
            printf '[%s]\n' "$section"
            printf 'url = %s\n' "$url"
            printf 'api_key = %s\n\n' "$ckey"
        } >> "$tmp"

        info "[$section] -> $c at $url"
    done

    if [ "$DRY_RUN" = "1" ]; then
        info "would write $CONF"
        rm -f "$tmp"
    else
        install -m 640 "$tmp" "$CONF"
        rm -f "$tmp"
    fi
fi

# --- bind-mount the guard dir into each container -------------------------

step "patching compose files"
CHANGED_DIRS=()

for c in "${CONTAINERS[@]}"; do
    if docker inspect "$c" --format '{{range .Mounts}}{{.Destination}} {{end}}' | grep -qw /guard; then
        info "$c already mounts /guard"
        continue
    fi

    work_dir=$(docker inspect "$c" \
               --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
    cfg_files=$(docker inspect "$c" \
               --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null || true)

    if [ -z "$work_dir" ]; then
        warn "$c was not started by docker compose. Add this to however you run it:"
        warn "    -v $GUARD_DIR:/guard"
        continue
    fi

    compose=""
    for candidate in ${cfg_files//,/ } docker-compose.yml compose.yaml docker-compose.yaml compose.yml; do
        [ -z "$candidate" ] && continue
        case "$candidate" in
            /*) path="$candidate" ;;
            *)  path="$work_dir/$candidate" ;;
        esac
        if [ -f "$path" ]; then compose="$path"; break; fi
    done

    [ -n "$compose" ] || { warn "$c: no compose file found under $work_dir"; continue; }

    if [ "$DRY_RUN" = "1" ]; then
        info "would patch $compose for $c"
        continue
    fi

    cp -a "$compose" "$compose.bak.$(date +%Y%m%d%H%M%S)"

    set +e
    python3 "$STAGE/lib/patch-compose.py" "$compose" "$c" "$GUARD_DIR"
    rc=$?
    set -e

    case "$rc" in
        0) info "$c: patched $compose"; CHANGED_DIRS+=("$work_dir") ;;
        3) info "$c: already mounted" ;;
        *) warn "$c: could not patch $compose, add '- $GUARD_DIR:/guard' by hand" ;;
    esac
done

# --- recreate only what changed -------------------------------------------

if [ ${#CHANGED_DIRS[@]} -eq 0 ]; then
    step "no compose changes, nothing to recreate"
else
    UNIQUE_DIRS=()
    while IFS= read -r d; do
        [ -n "$d" ] && UNIQUE_DIRS+=("$d")
    done < <(printf '%s\n' "${CHANGED_DIRS[@]}" | sort -u)

    step "recreating ${#UNIQUE_DIRS[@]} compose project(s)"
    for d in "${UNIQUE_DIRS[@]}"; do
        info "$d"
        run bash -c "cd '$d' && docker compose up -d"
    done

    info "waiting for Sonarr to come back up"
    run sleep 20
fi

# --- cron ------------------------------------------------------------------

if [ "${SONARR_GUARD_CRON:-1}" = "1" ]; then
    step "cron"
    PY="$(command -v python3)"
    HOURLY="0 * * * * $PY $GUARD_DIR/sonarr-airdate-guard.py >/dev/null 2>&1"
    WEEKLY="30 4 * * 0 $PY $GUARD_DIR/sonarr-airdate-guard.py --files-only --scope imported_early >/dev/null 2>&1"

    if [ "$DRY_RUN" = "1" ]; then
        info "would install: $HOURLY"
        info "would install: $WEEKLY"
    else
        current="$(crontab -l 2>/dev/null || true)"
        cleaned="$(printf '%s\n' "$current" | grep -v 'sonarr-airdate-guard.py' || true)"
        printf '%s\n%s\n%s\n' "$cleaned" "$HOURLY" "$WEEKLY" | grep -v '^[[:space:]]*$' | crontab -
        info "hourly: queue guard plus unaired file cleanup"
        info "weekly: deep imported-early sweep, Sundays 04:30"
    fi
else
    step "cron skipped (SONARR_GUARD_CRON=0)"
fi

# --- verify ----------------------------------------------------------------

if [ "$DRY_RUN" = "1" ]; then
    printf '\nDry run finished. Nothing was changed.\n'
    exit 0
fi

step "config check"
python3 "$GUARD_DIR/sonarr_guard_common.py" || true

step "wiring the Custom Script connection into each Sonarr"
python3 "$GUARD_DIR/lib/setup-connections.py" || true

step "guard directory visible inside each container"
for c in "${CONTAINERS[@]}"; do
    printf '    %-22s ' "$c"
    if docker exec "$c" test -r /guard/sonarr-guard.conf 2>/dev/null; then
        printf '/guard ok, Arguments: %s\n' "$(section_for "$c")"
    else
        printf '/guard NOT VISIBLE\n'
    fi
done

cat <<NOTE

==> before you let it loose, look at what it would do

      python3 $GUARD_DIR/sonarr-airdate-guard.py --dry-run

  That reports every queue item it would remove, every file it would delete, and
  every episode it would unmonitor, and changes nothing.

==> the connections are already set up

  Nothing to click. The Custom Script connection was created on each instance by
  the step above. Sonarr 4.0.19 dropped support for the Arguments field, so the
  hook identifies which instance it belongs to on its own.

==> if you ever uninstall

      python3 $GUARD_DIR/sonarr-airdate-guard.py --restore-monitoring
      python3 $GUARD_DIR/lib/setup-connections.py --remove

  The first gives back the monitoring on every episode this tool unmonitored.
  Skip it and those episodes stay unmonitored forever.

NOTE
