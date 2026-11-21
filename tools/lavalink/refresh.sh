#!/bin/bash
# Installed root-owned alongside release.py. All JAR and config access is unprivileged.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
as_owner() { runuser -u "$owner" -- /usr/bin/python3 -I "$helper" "$1" "$dir"; }

connected() {
    local peers bot pid
    peers=$(ss -Htnp state established "( dport = :$port )") || return 1
    for bot in "${bots[@]}"; do
        systemctl is-active --quiet "$bot" || return 1
        pid=$(systemctl show -p MainPID --value "$bot") || return 1
        [[ "$pid" != 0 && "$peers" == *"pid=$pid,"* ]] || return 1
    done
}
check_voice() {
    local socket rc
    for socket in ${RED_PLAYBACK_SOCKETS:-}; do
        rc=0
        runuser -u "$owner" -- /usr/bin/python3 -I "$(dirname "$helper")/probe.py" "$@" "$socket" || rc=$?
        if ((rc != 0)); then return "$rc"; fi
    done
    return 0
}
verify_installed() {
    local i idle_rc voice_rc
    if ! as_owner running; then
        idle_rc=0
        as_owner idle || idle_rc=$?
        if ((idle_rc == 75)); then return 75; fi
        if ((idle_rc != 0)) && connected; then
            echo "Waiting for Lavalink statistics before restarting a connected node."
            return 75
        fi
        if ((idle_rc == 0)); then
            voice_rc=0
            check_voice --available || voice_rc=$?
            if ((voice_rc == 75)); then return 75; fi
        fi
        systemctl restart "$unit" || return 1
    fi
    for ((i=0; i<60; i++)); do
        if connected && as_owner running; then
            as_owner idle || return $?
            as_owner check || return 1
            check_voice || return $?
            return 0
        fi
        sleep 2
    done
    return 1
}
refresh() {
    local phase rc
    phase=$(as_owner phase) || return 1
    if [[ "$phase" == none ]]; then
        connected || { echo "Deferring update while the RedBot clients are disconnected."; return 0; }
        rc=0
        check_voice --available || rc=$?
        if ((rc == 75)); then return 0; elif ((rc)); then return "$rc"; fi
        as_owner update || rc=$?
        case "$rc" in
            10) return 0 ;;
            0|12) phase=$(as_owner phase) || return 1 ;;
            *) return "$rc" ;;
        esac
    fi
    if [[ "$phase" == candidate ]]; then
        rc=0
        verify_installed || rc=$?
        case "$rc" in
            0) as_owner commit; return $? ;;
            75) echo "Candidate verification deferred until idle."; return 0 ;;
        esac
    elif [[ "$phase" != rollback ]]; then
        echo "Unexpected Lavalink update phase: $phase" >&2
        return 1
    fi
    echo "Verifying recovery with the previous Lavalink JAR." >&2
    as_owner rollback || return 1
    rc=0
    verify_installed || rc=$?
    if ((rc == 75)); then echo "Rollback verification deferred until idle."; return 0; fi
    if ((rc)); then return "$rc"; fi
    as_owner commit || return 1
    echo "Restored and verified the previous Lavalink JAR." >&2
    # Surface the failed release on the run that observed its failure.
    [[ "$phase" == rollback ]]
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    unit=${1:?Lavalink service required}
    shift
    (( $# )) || { echo "Specify the RedBot service units that use Lavalink." >&2; exit 1; }
    bots=("$@")
    owner=$(systemctl show -p User --value "$unit")
    dir=$(systemctl show -p WorkingDirectory --value "$unit")
    [[ -n "$owner" && $(id -u "$owner") != 0 && "$dir" == /* ]]
    helper="$(dirname "$(realpath "$0")")/release.py"
    exec 9>/run/lock/redbot-audio-maintenance.lock
    flock -n 9 || exit 0
    port=$(as_owner port)
    [[ "$port" =~ ^[0-9]+$ ]]
    refresh
fi
