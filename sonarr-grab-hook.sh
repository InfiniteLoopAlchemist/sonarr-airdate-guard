#!/bin/bash
# sonarr-grab-hook.sh
#
# Sonarr Custom Script, multi-instance aware. Runs INSIDE the Sonarr container.
# The linuxserver images are Alpine with no python3, so this is bash + curl + jq,
# all three of which the image already ships.
#
#   Sonarr > Settings > Connect > + > Custom Script
#       Name:        Air Date Guard
#       On Grab:     yes
#       On Import:   yes      (Sonarr calls this event "Download")
#       everything else: no
#       Path:        /guard/sonarr-grab-hook.sh
#
# There is no Arguments field to fill in. Sonarr 4.0.19 removed argument support
# from custom scripts, so the hook identifies itself: it reads its own key and
# port out of /config/config.xml and matches them against the shared config. An
# argument is still honoured if you pass one, for older Sonarr builds.
#
# On Grab   an early release is pulled from the queue and blocklisted before it
#           finishes downloading.
# On Import an early file that slipped through anyway is deleted from disk and
#           its release blocklisted, immediately, instead of waiting for the
#           hourly poller.
#
# The instance's own URL and key come from /config/config.xml, so regenerating
# the key in the UI cannot desync this script. Everything else comes from the
# shared /guard/sonarr-guard.conf.
#
# This never exits non-zero. A guard that wedges Sonarr's notification thread
# is worse than a guard that quietly gives up.

CONF="${SONARR_GUARD_CONFIG:-/guard/sonarr-guard.conf}"
SONARR_CONFIG_XML="${SONARR_CONFIG_XML:-/config/config.xml}"
MAX_RUNTIME=45
QUEUE_POLL_INTERVAL=2

STARTED=$(date +%s)
SECTION="$1"

# --- logging ---------------------------------------------------------------

LOG=""
for candidate in "${SONARR_GUARD_LOG:-}" /guard/sonarr-guard.log /config/sonarr-guard.log /tmp/sonarr-guard.log; do
    [ -z "$candidate" ] && continue
    if touch "$candidate" 2>/dev/null; then
        LOG="$candidate"
        break
    fi
done

say() {
    local line
    line="[$(date '+%Y-%m-%d %H:%M:%S')][hook][${SECTION:-?}] $*"
    # stderr, never stdout. Functions like find_queue_id return their value on
    # stdout, and a log line landing in a $(...) capture corrupts it silently.
    # Sonarr records both streams, so nothing is lost by using stderr.
    echo "$line" >&2
    [ -n "$LOG" ] && echo "$line" >> "$LOG" 2>/dev/null
    return 0
}

die_quietly() {
    say "$*"
    exit 0
}

# --- Sonarr passes its variables through a .NET StringDictionary, which
# --- lowercases the keys on some builds and not others. Normalise both.

for _name in $(compgen -e); do
    case "$_name" in
        [Ss][Oo][Nn][Aa][Rr][Rr]_*)
            _lower="g_$(printf '%s' "$_name" | tr '[:upper:]' '[:lower:]')"
            printf -v "$_lower" '%s' "${!_name}"
            ;;
    esac
done

# --- config ----------------------------------------------------------------

conf_get() {
    awk -v want="$SECTION" -v key="$1" '
        /^[[:space:]]*[#;]/ { next }
        /^[[:space:]]*\[/ {
            sec = $0
            sub(/^[[:space:]]*\[/, "", sec)
            sub(/\][[:space:]]*$/, "", sec)
            next
        }
        {
            eq = index($0, "=")
            if (eq == 0) next
            k = substr($0, 1, eq - 1)
            v = substr($0, eq + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
            if (k != key) next
            if (sec == "DEFAULT") d = v
            else if (tolower(sec) == tolower(want)) { s = v; seen = 1 }
        }
        END { if (seen) print s; else print d }
    ' "$CONF"
}

conf_has_section() {
    awk -v want="$SECTION" '
        /^[[:space:]]*\[/ {
            sec = $0
            sub(/^[[:space:]]*\[/, "", sec)
            sub(/\][[:space:]]*$/, "", sec)
            if (sec != "DEFAULT" && tolower(sec) == tolower(want)) { found = 1 }
        }
        END { exit(found ? 0 : 1) }
    ' "$CONF"
}

conf_sections() {
    awk '
        /^[[:space:]]*\[/ {
            sec = $0
            sub(/^[[:space:]]*\[/, "", sec)
            sub(/\][[:space:]]*$/, "", sec)
            if (sec != "DEFAULT") printf "%s ", sec
        }
    ' "$CONF"
}

conf_section_count() {
    conf_sections | wc -w
}

# Find the section whose <key> equals <value>. Used to work out which instance
# we are without being told.
conf_section_where() {
    awk -v key="$1" -v want="$2" '
        /^[[:space:]]*[#;]/ { next }
        /^[[:space:]]*\[/ {
            sec = $0
            sub(/^[[:space:]]*\[/, "", sec)
            sub(/\][[:space:]]*$/, "", sec)
            next
        }
        {
            eq = index($0, "=")
            if (eq == 0) next
            k = substr($0, 1, eq - 1)
            v = substr($0, eq + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
            if (sec == "DEFAULT" || sec == "") next
            if (k == key && v == want) { print sec; exit }
        }
    ' "$CONF"
}

# Same idea, but matches a url ending in :<port>, with or without a trailing
# path, for instances whose published port differs from the internal one.
conf_section_by_port() {
    awk -v want="$1" '
        /^[[:space:]]*[#;]/ { next }
        /^[[:space:]]*\[/ {
            sec = $0
            sub(/^[[:space:]]*\[/, "", sec)
            sub(/\][[:space:]]*$/, "", sec)
            next
        }
        {
            eq = index($0, "=")
            if (eq == 0) next
            k = substr($0, 1, eq - 1)
            v = substr($0, eq + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
            if (sec == "DEFAULT" || sec == "") next
            if (k == "url" && (v ~ (":" want "$") || v ~ (":" want "/"))) { print sec; exit }
        }
    ' "$CONF"
}

is_true() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on|enabled) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Sonarr's own connection details, straight from its config file ---------

read_self() {
    [ -r "$SONARR_CONFIG_XML" ] || return 1
    SELF_KEY=$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$SONARR_CONFIG_XML" | head -1)
    SELF_PORT=$(sed -n 's:.*<Port>\(.*\)</Port>.*:\1:p' "$SONARR_CONFIG_XML" | head -1)
    local url_base
    url_base=$(sed -n 's:.*<UrlBase>\(.*\)</UrlBase>.*:\1:p' "$SONARR_CONFIG_XML" | head -1)
    [ -n "$SELF_KEY" ] && [ -n "$SELF_PORT" ] || return 1
    url_base="${url_base#/}"
    [ -n "$url_base" ] && url_base="/$url_base"
    BASE="http://127.0.0.1:${SELF_PORT}${url_base}"
    return 0
}

# Which config section describes this container?
#
# Sonarr 4.0.19 removed the Arguments field from custom scripts ("Arguments are
# no longer supported for custom scripts"), so the hook cannot be told which
# instance it belongs to. It works it out instead, and only falls back to an
# explicit argument for older builds that still allow one.
resolve_section() {
    local found

    # 1. an explicit argument, on builds that still support it
    if [ -n "$SECTION" ]; then
        SECTION_SOURCE="argument"
        return 0
    fi

    # 2. the key this container is actually using. Exact and unambiguous.
    found=$(conf_section_where api_key "$SELF_KEY")
    if [ -n "$found" ]; then
        SECTION="$found"; SECTION_SOURCE="api key"
        return 0
    fi

    # 3. a url ending in this container's port
    found=$(conf_section_by_port "$SELF_PORT")
    if [ -n "$found" ]; then
        SECTION="$found"; SECTION_SOURCE="port $SELF_PORT"
        return 0
    fi

    # 4. Sonarr's own instance name, if it happens to match a section
    if [ -n "${g_sonarr_instancename}" ] && SECTION="${g_sonarr_instancename}" && conf_has_section; then
        SECTION_SOURCE="instance name"
        return 0
    fi
    SECTION=""

    # 5. only one instance configured, so there is nothing to disambiguate
    if [ "$(conf_section_count)" = "1" ]; then
        SECTION="$(conf_sections)"
        SECTION="${SECTION%% *}"
        SECTION_SOURCE="the only configured instance"
        return 0
    fi

    return 1
}

api() {
    local method="$1" path="$2" timeout="${3:-15}"
    curl -fsS -m "$timeout" -X "$method" \
        -H "X-Api-Key: $SELF_KEY" -H "Accept: application/json" \
        "${BASE}${path}"
}

# Same call, but prints only the HTTP status. Needed where the difference
# between "rejected" and "already gone" changes what we log.
api_status() {
    local method="$1" path="$2" timeout="${3:-15}"
    curl -sS -m "$timeout" -o /dev/null -w '%{http_code}' -X "$method" \
        -H "X-Api-Key: $SELF_KEY" -H "Accept: application/json" \
        "${BASE}${path}" 2>/dev/null
}

# --- date helpers ----------------------------------------------------------

to_epoch() {
    date -u -d "$1" +%s 2>/dev/null
}

# Reads a comma or pipe separated air-date list and prints the reference epoch:
# the latest date under strict, the earliest under lenient.
reference_epoch() {
    local raw="$1" mode="$2" ref="" epoch chunk
    local IFS=',|'
    for chunk in $raw; do
        chunk="${chunk#"${chunk%%[![:space:]]*}"}"
        chunk="${chunk%"${chunk##*[![:space:]]}"}"
        [ -z "$chunk" ] && continue
        epoch=$(to_epoch "$chunk") || continue
        [ -z "$epoch" ] && continue
        if [ -z "$ref" ]; then
            ref="$epoch"
        elif [ "$mode" = "lenient" ]; then
            [ "$epoch" -lt "$ref" ] && ref="$epoch"
        else
            [ "$epoch" -gt "$ref" ] && ref="$epoch"
        fi
    done
    printf '%s' "$ref"
}

pretty() { date -u -d "@$1" '+%Y-%m-%d' 2>/dev/null; }

remaining() {
    local left=$(( STARTED + MAX_RUNTIME - $(date +%s) ))
    printf '%s' "$(( left < 0 ? 0 : left ))"
}

# --- actions ---------------------------------------------------------------

blocklist_episode() {
    local episode_id="$1" history_id source_title body
    [ -z "$episode_id" ] && return 1

    body=$(api GET "/api/v3/history?episodeId=${episode_id}&page=1&pageSize=50&sortKey=date&sortDirection=descending" 20) || return 1
    history_id=$(printf '%s' "$body" | jq -r '[.records[] | select(.eventType == "grabbed" or .eventType == 1)] | first | .id // empty')
    [ -z "$history_id" ] && return 1
    source_title=$(printf '%s' "$body" | jq -r '[.records[] | select(.eventType == "grabbed" or .eventType == 1)] | first | .sourceTitle // empty')

    api POST "/api/v3/history/failed/${history_id}" 20 >/dev/null || return 1
    say "  -> blocklisted the release: ${source_title:-$history_id}"
    return 0
}

find_queue_id() {
    local download_id="$1" release_title="$2" attempt=0 body qid
    while [ "$(remaining)" -gt "$QUEUE_POLL_INTERVAL" ]; do
        attempt=$((attempt + 1))
        body=$(api GET "/api/v3/queue?page=1&pageSize=200&includeEpisode=true" 10)
        if [ -n "$body" ]; then
            # Match on the download id, case insensitively because clients differ
            # on hex casing, and fall back to the release title.
            qid=$(printf '%s' "$body" | jq -r \
                --arg dl "$download_id" --arg title "$release_title" '
                [ .records[]
                  | select(
                      (($dl    != "") and (((.downloadId // "") | ascii_downcase) == ($dl | ascii_downcase)))
                      or
                      (($title != "") and ((.title // "") == $title))
                    )
                ] | first | .id // empty')
            if [ -n "$qid" ]; then
                say "  -> found queue id $qid after $attempt attempt(s)"
                printf '%s' "$qid"
                return 0
            fi
        fi
        sleep "$QUEUE_POLL_INTERVAL"
    done
    return 1
}

# Returns 0 removed, 1 already gone, 2 rejected. REMOVE_DETAIL carries the code.
#
# skipRedownload=true matters more than it looks. Sonarr's "Redownload Failed"
# setting is on by default, so a plain blocklist makes it immediately search for
# a replacement. For an unaired episode the replacement is just the next fake, so
# blocklisting without this flag builds a loop that never terminates: grab,
# block, search, grab. We never want a replacement for an episode that has not
# aired, so the search is always skipped.
remove_from_queue() {
    local qid="$1" code
    REMOVE_DETAIL=""

    code=$(api_status DELETE "/api/v3/queue/${qid}?removeFromClient=true&blocklist=true&skipRedownload=true" 20)
    case "$code" in
        2*) return 0 ;;
        404) REMOVE_DETAIL="already left the queue"; return 1 ;;
    esac

    # Sonarr v3 spelled the parameter blacklist.
    code=$(api_status DELETE "/api/v3/queue/${qid}?removeFromClient=true&blacklist=true&skipRedownload=true" 20)
    case "$code" in
        2*) return 0 ;;
        404) REMOVE_DETAIL="already left the queue"; return 1 ;;
    esac

    # The download client can refuse the removal even though Sonarr is willing
    # to drop the queue item. Blocklisting still matters, so try without it.
    code=$(api_status DELETE "/api/v3/queue/${qid}?removeFromClient=false&blocklist=true&skipRedownload=true" 20)
    case "$code" in
        2*) REMOVE_DETAIL="left in the download client"; return 0 ;;
        404) REMOVE_DETAIL="already left the queue"; return 1 ;;
    esac

    REMOVE_DETAIL="HTTP $code"
    return 2
}

# --- main ------------------------------------------------------------------

EVENT="${g_sonarr_eventtype}"

if [ ! -r "$CONF" ]; then
    die_quietly "ERROR: cannot read $CONF. Is the guard directory mounted into this container?"
fi

if ! read_self; then
    die_quietly "ERROR: could not read the key and port from $SONARR_CONFIG_XML"
fi

if ! resolve_section; then
    die_quietly "ERROR: cannot tell which instance this is. No section in $CONF has this container's key or port $SELF_PORT. Sections are: $(conf_sections)"
fi

if ! conf_has_section; then
    die_quietly "ERROR: no section [$SECTION] in $CONF. Sections are: $(conf_sections)"
fi

GRACE_DAYS=$(conf_get grace_days); GRACE_DAYS="${GRACE_DAYS:-3}"
MODE=$(printf '%s' "$(conf_get mode)" | tr '[:upper:]' '[:lower:]'); MODE="${MODE:-strict}"
ENABLED=$(conf_get enabled); ENABLED="${ENABLED:-true}"
DELETE_FILES=$(conf_get delete_early_files); DELETE_FILES="${DELETE_FILES:-true}"
DO_BLOCKLIST=$(conf_get delete_blocklist); DO_BLOCKLIST="${DO_BLOCKLIST:-true}"

if [ "$EVENT" = "Test" ]; then
    VERSION=$(api GET "/api/v3/system/status" 10 | jq -r '.version // "unknown"')
    if [ -z "$VERSION" ] || [ "$VERSION" = "unknown" ]; then
        say "TEST FAILED: matched [$SECTION] but could not reach $BASE"
        exit 0
    fi
    say "Test OK. Matched [$SECTION] by ${SECTION_SOURCE}, on $BASE (Sonarr $VERSION), grace ${GRACE_DAYS}d, mode $MODE, delete_early_files=$DELETE_FILES"
    exit 0
fi

if ! is_true "$ENABLED"; then
    die_quietly "Instance disabled in config, taking no action"
fi

case "$EVENT" in

Grab)
    SERIES="${g_sonarr_series_title:-Unknown Series}"
    RELEASE="${g_sonarr_release_title:-<untitled>}"
    LABEL="$SERIES S${g_sonarr_release_seasonnumber:-?}E${g_sonarr_release_episodenumbers:-?}"
    DOWNLOAD_ID="${g_sonarr_download_id}"

    REF=$(reference_epoch "${g_sonarr_release_episodeairdatesutc}" "$MODE")
    if [ -z "$REF" ]; then
        die_quietly "ALLOW $LABEL | no usable air date in grab payload | $RELEASE"
    fi

    NOW=$(date +%s)
    THRESHOLD=$(awk -v r="$REF" -v g="$GRACE_DAYS" 'BEGIN { printf "%d", r + g * 86400 }')

    if [ "$NOW" -ge "$THRESHOLD" ]; then
        die_quietly "ALLOW $LABEL | aired $(pretty "$REF"), grace cleared | $RELEASE"
    fi

    LEFT=$((THRESHOLD - NOW))
    say "EARLY $LABEL | air date $(pretty "$REF") | clears in $((LEFT / 86400))d $(((LEFT % 86400) / 3600))h | $RELEASE"

    if [ -z "$DOWNLOAD_ID" ] && [ -z "$RELEASE" ]; then
        die_quietly "  -> nothing in the payload to match against the queue, leaving it for the import hook"
    fi

    QID=$(find_queue_id "$DOWNLOAD_ID" "$RELEASE")
    if [ -z "$QID" ]; then
        # Usenet grabs of small files can finish and import inside the time
        # budget, which empties the queue before we ever see the item. The
        # import hook catches those, so this is a note and not a failure.
        die_quietly "  -> not in the queue within ${MAX_RUNTIME}s, most likely already imported. The import hook covers it."
    fi

    remove_from_queue "$QID"
    case "$?" in
        0) say "  -> removed and blocklisted in $(( $(date +%s) - STARTED ))s${REMOVE_DETAIL:+ ($REMOVE_DETAIL)}" ;;
        1) say "  -> $REMOVE_DETAIL, the import hook covers it" ;;
        *) say "  -> removal rejected ($REMOVE_DETAIL), leaving it for the poller" ;;
    esac
    ;;

Download)
    # Sonarr calls the import event "Download". A file that reached the library
    # for an episode that has not aired is the case the grab guard missed.
    if ! is_true "$DELETE_FILES"; then
        die_quietly "Import of ${g_sonarr_series_title}: file deletion disabled in config"
    fi

    SERIES="${g_sonarr_series_title:-Unknown Series}"
    LABEL="$SERIES S${g_sonarr_episodefile_seasonnumber:-?}E${g_sonarr_episodefile_episodenumbers:-?}"
    FILE_ID="${g_sonarr_episodefile_id}"
    FILE_PATH="${g_sonarr_episodefile_relativepath:-<unknown path>}"

    REF=$(reference_epoch "${g_sonarr_episodefile_episodeairdatesutc}" "$MODE")
    if [ -z "$REF" ]; then
        die_quietly "KEEP $LABEL | no usable air date on import | $FILE_PATH"
    fi

    NOW=$(date +%s)
    # No grace here. A file for an episode that aired two hours ago may be real;
    # a file for an episode that has not aired cannot be.
    if [ "$NOW" -ge "$REF" ]; then
        exit 0
    fi

    AHEAD=$((REF - NOW))
    say "EARLY IMPORT $LABEL | air date $(pretty "$REF") | $((AHEAD / 86400))d $(((AHEAD % 86400) / 3600))h before air | upgrade=${g_sonarr_isupgrade:-False} | $FILE_PATH"

    if [ -z "$FILE_ID" ]; then
        die_quietly "  -> no episode file id in payload, leaving it for the poller"
    fi

    if api DELETE "/api/v3/episodefile/${FILE_ID}" 30 >/dev/null; then
        say "  -> deleted from disk"
    else
        die_quietly "  -> delete failed, leaving it for the poller"
    fi

    if is_true "$DO_BLOCKLIST"; then
        FIRST_EPISODE="${g_sonarr_episodefile_episodeids%%,*}"
        blocklist_episode "$FIRST_EPISODE" || say "  -> could not blocklist the release, Sonarr may grab it again"
    fi
    ;;

*)
    say "Ignoring event type '${EVENT:-<none>}'"
    ;;
esac

exit 0
