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
# Resolve custom socket paths from the live unit without exposing its environment.
socket_for_unit() {
    local unit=$1 pid entry
    pid=$(systemctl show -p MainPID --value "$unit") || return 1
    [[ "$pid" =~ ^[1-9][0-9]*$ && -r /proc/$pid/environ ]] || return 1
    while IFS= read -r -d '' entry; do
        if [[ $entry == RED_AUDIO_PROBE_SOCKET=* ]]; then
            printf '%s\n' "${entry#RED_AUDIO_PROBE_SOCKET=}"
            return 0
        fi
    done <"/proc/$pid/environ"
    return 1
}
voice_socket_ready() {
    local bot=$1 socket=$2 user uid expected
    user=$(systemctl show -p User --value "$bot") || return 1
    [[ -n "$user" ]] || return 1
    uid=$(id -u "$user") || return 1
    expected=$(socket_for_unit "$bot") || return 1
    [[ "$uid" != 0 && "$socket" == "$expected" && -S "$socket" &&
        $(stat -c %u -- "$(dirname -- "$socket")") == "$uid" ]]
}
verified_voice() {
    # A sourced service wrapper may supply its own account-aware check_voice.
    # Always validate the complete, ordered target set before dispatching to it.
    local i socket
    local -a sockets
    local -A seen=() units=()
    read -r -a sockets <<<"${RED_PLAYBACK_SOCKETS:-}"
    if [[ -z ${bots[0]:-} || ${RED_PLAYBACK_SOCKETS:-} == *$'\n'* ]] ||
       ((${#sockets[@]} != ${#bots[@]})); then
        echo "Playback unverified: configure one test socket per Red unit in RED_PLAYBACK_SOCKETS." >&2
        return 78
    fi
    for i in "${!bots[@]}"; do
        socket=${sockets[i]}
        if [[ "$socket" != /* || -n ${seen[$socket]:-} || -n ${units[${bots[i]}]:-} ]]; then
            echo "Playback unverified: each Red unit needs its own absolute test socket path." >&2
            return 78
        fi
        seen[$socket]=1
        units[${bots[i]}]=1
        if ! voice_socket_ready "${bots[i]}" "$socket"; then
            echo "Playback unverified: check the configured socket and owner for ${bots[i]}." >&2
            return 78
        fi
    done
    check_voice "$@"
}
check_voice() {
    local i socket user rc
    local -a sockets
    read -r -a sockets <<<"${RED_PLAYBACK_SOCKETS:-}"
    for i in "${!bots[@]}"; do
        socket=${sockets[i]}
        user=$(systemctl show -p User --value "${bots[i]}") || return 1
        [[ -n "$user" && $(id -u "$user") != 0 ]] || return 1
        rc=0
        runuser -u "$user" -- /usr/bin/python3 -I "$(dirname "$helper")/probe.py" "$@" "$socket" || rc=$?
        if ((rc != 0)); then return "$rc"; fi
    done
    return 0
}
verify_installed() {
    local mode=$1 i idle_rc voice_rc
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
            verified_voice --available || voice_rc=$?
            if ((voice_rc == 75)); then return 75; fi
            if [[ "$mode" == candidate ]] && ((voice_rc != 0)); then return "$voice_rc"; fi
        fi
        systemctl restart "$unit" || return 1
    fi
    for ((i=0; i<60; i++)); do
        if connected && as_owner running; then
            as_owner idle || return $?
            as_owner check || return 1
            if [[ "$mode" == candidate ]]; then
                verified_voice --require-playback || return $?
            else
                # Recovery can finish on an older bot with the original probe protocol.
                voice_rc=0
                verified_voice || voice_rc=$?
                if ((voice_rc != 0 && voice_rc != 78)); then return "$voice_rc"; fi
            fi
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
        verified_voice --available || rc=$?
        if ((rc == 75 || rc == 78)); then return 0; elif ((rc)); then return "$rc"; fi
        as_owner update || rc=$?
        case "$rc" in
            10) return 0 ;;
            0|12) phase=$(as_owner phase) || return 1 ;;
            *) return "$rc" ;;
        esac
    fi
    if [[ "$phase" == candidate ]]; then
        rc=0
        verify_installed candidate || rc=$?
        case "$rc" in
            0) as_owner commit; return $? ;;
            75) echo "Candidate verification deferred until idle."; return 0 ;;
            78) echo "Candidate verification deferred until test channels are configured."; return 0 ;;
        esac
    elif [[ "$phase" != rollback ]]; then
        echo "Unexpected Lavalink update phase: $phase" >&2
        return 1
    fi
    echo "Verifying recovery with the previous Lavalink JAR." >&2
    as_owner rollback || return 1
    rc=0
    verify_installed rollback || rc=$?
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
