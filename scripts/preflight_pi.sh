#!/usr/bin/env bash
# Read-only checks for the Pi's TP-Link Archer T3U / rtw88_8822bu host setup.
set -euo pipefail

# Debian may omit sbin directories from PATH for non-interactive SSH commands,
# even though tools such as iw and modinfo are installed there.  Preflight is
# normally launched that way by the desktop workflow, so use the normal system
# command locations explicitly.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PYTHON=${PYTHON:-"$PROJECT_ROOT/.venv/bin/python"}
FAILURES=0

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    FAILURES=$((FAILURES + 1))
}

pass() {
    printf 'OK: %s\n' "$*"
}

if [[ ! -x "$PYTHON" ]]; then
    fail "virtual environment not found; run scripts/setup_pi.sh"
elif ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    fail "Python 3.11+ is required"
else
    pass "Python $($PYTHON -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
fi

if [[ ! -d "$PROJECT_ROOT/vendor/LDN/ldn" ]]; then
    fail "vendored LDN is missing from $PROJECT_ROOT/vendor/LDN"
elif [[ -x "$PYTHON" ]] && "$PYTHON" - "$PROJECT_ROOT" <<'PY'
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "vendor" / "LDN"))
import ldn
if root / "vendor" / "LDN" not in pathlib.Path(ldn.__file__).resolve().parents:
    raise SystemExit(1)
print(pathlib.Path(ldn.__file__).resolve())
PY
then
    pass "LDN resolves to vendored source"
else
    fail "Python does not resolve ldn from vendor/LDN"
fi

CONFIG_VALUES=""
USE_EXPLICIT_PHY=false
if [[ -x "$PYTHON" ]]; then
    if "$PYTHON" "$PROJECT_ROOT/frlgmg_host.py" \
            --print-effective-config "$@" >/dev/null; then
        pass "Mystery Gift CLI accepts the effective TOML configuration"
    else
        fail "Mystery Gift CLI cannot load the effective TOML configuration"
    fi
    if ! CONFIG_VALUES=$("$PYTHON" - "$PROJECT_ROOT" "$@" <<'PY'
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import frlgmg_host
from frlgsim import host_cli

argv = sys.argv[2:]
file_config, shared_path, local_path = host_cli.load_host_file_config_from_argv(argv)
parser = frlgmg_host.build_parser(
    file_config, shared_path=shared_path, local_path=local_path)
args = parser.parse_args(argv)
_profile, ldn, options = host_cli.build_host_config(parser, args)
print(ldn.adapter)
print(ldn.phy)
print(ldn.keys_path)
print(pathlib.Path(ldn.keys_path).expanduser())
print(str(args.live).lower())
print(str(options.skip_encryption).lower())
print(str(options.accept_decrypted_ccmp).lower())
PY
); then
        fail "unable to load config/host.toml (and optional host.local.toml)"
    fi
fi

if [[ -n "$CONFIG_VALUES" ]]; then
    mapfile -t CONFIG_LINES <<<"$CONFIG_VALUES"
    ADAPTER=${CONFIG_LINES[0]}
    CONFIG_PHY=${CONFIG_LINES[1]}
    CONFIG_KEYS_PATH=${CONFIG_LINES[2]}
    KEYS_PATH=${CONFIG_LINES[3]}
    LIVE=${CONFIG_LINES[4]}
    SKIP_ENCRYPTION=${CONFIG_LINES[5]}
    ACCEPT_DECRYPTED_CCMP=${CONFIG_LINES[6]}
    if [[ "$CONFIG_PHY" != "auto" ]]; then
        USE_EXPLICIT_PHY=true
    fi
    if [[ "$LIVE" == true && "$SKIP_ENCRYPTION" == true ]]; then
        pass "live hosting and delegated transmit CCMP are enabled"
    else
        fail "host profile requires live=true and skip_encryption=true"
    fi
    if [[ "$USE_EXPLICIT_PHY" == false ]]; then
        # The adapter's phy is resolved by driver below; per-driver CCMP rules
        # are validated there, so no adapter-specific assertion is needed here.
        pass "resolving host phy automatically from adapter profile '$ADAPTER'"
    else
        pass "configured explicit host phy $CONFIG_PHY (named adapter is bypassed)"
    fi
else
    KEYS_PATH=""
    CONFIG_KEYS_PATH=""
    CONFIG_PHY="auto"
fi

# Mirror transport.HOST_ADAPTER_PROFILES: map an adapter profile name to the
# kernel driver that identifies it. Empty for an unknown profile.
adapter_driver() {
    case "$1" in
        mt7601u) echo "mt7601u" ;;
        tplink-archer-t3u) echo "rtw88_8822bu" ;;
        *) echo "" ;;
    esac
}

# First phy currently bound to the given driver (survives phy renumbering).
find_phy_by_driver() {
    local want=$1 phy_dir phy drv
    for phy_dir in /sys/class/ieee80211/phy*; do
        [[ -e "$phy_dir" ]] || continue
        phy=$(basename "$phy_dir")
        drv=""
        [[ -L "$phy_dir/device/driver" ]] \
            && drv=$(basename "$(readlink -f "$phy_dir/device/driver")")
        if [[ "$drv" == "$want" ]]; then
            echo "$phy"
            return 0
        fi
    done
    return 1
}

check_phy_modes() {
    local phy=$1 label=$2 phy_info
    if ! command -v iw >/dev/null 2>&1; then
        fail "iw is not installed"
        return
    fi
    phy_info=$(iw phy "$phy" info 2>/dev/null || true)
    if grep -qE '^[[:space:]]*\* AP$' <<<"$phy_info"; then
        pass "$label supports AP mode"
    else
        fail "$label does not report AP mode"
    fi
    if grep -qE '^[[:space:]]*\* monitor$' <<<"$phy_info"; then
        pass "$label supports monitor mode"
    else
        fail "$label does not report monitor mode"
    fi
}

# Validate a chosen phy's driver-specific CCMP rule and its AP/monitor modes.
validate_selected_phy() {
    local phy=$1 driver_link driver module_path
    driver_link="/sys/class/ieee80211/$phy/device/driver"
    driver=""
    [[ -L "$driver_link" ]] && driver=$(basename "$(readlink -f "$driver_link")")
    pass "selected phy is $phy (${driver:-unknown} driver)"
    case "$driver" in
        mt76x0u)
            if [[ "$ACCEPT_DECRYPTED_CCMP" == false ]]; then
                pass "mt76x0u uses standard CCMP receive frames"
            else
                fail "mt76x0u requires accept_decrypted_ccmp=false"
            fi
            ;;
        mt7601u)
            if [[ "$ACCEPT_DECRYPTED_CCMP" == false ]]; then
                pass "mt7601u uses standard CCMP receive frames"
            else
                fail "mt7601u requires accept_decrypted_ccmp=false"
            fi
            module_path=$(modinfo -k "$(uname -r)" -n mt7601u 2>/dev/null || true)
            if [[ "$module_path" == */updates/dkms/mt7601u.ko* ]]; then
                pass "mt7601u AP-mode DKMS module is installed"
            else
                fail "mt7601u stock module is active; install the AP-mode driver with scripts/setup_pi.sh --install-mt7601u-ap"
            fi
            ;;
        rtw88_8822bu)
            if [[ "$ACCEPT_DECRYPTED_CCMP" == true ]]; then
                pass "rtw88_8822bu retained-CCMP receive normalization is enabled"
            else
                fail "rtw88_8822bu requires accept_decrypted_ccmp=true"
            fi
            ;;
        *)
            pass "selected phy has no built-in CCMP receive profile"
            ;;
    esac
    check_phy_modes "$phy" "selected phy $phy"
}

SELECTED_PHY=""
if [[ "$USE_EXPLICIT_PHY" == true ]]; then
    SELECTED_PHY=$CONFIG_PHY
    if [[ ! -d "/sys/class/ieee80211/$SELECTED_PHY" ]]; then
        fail "configured phy $SELECTED_PHY does not exist"
        SELECTED_PHY=""
    fi
else
    WANT_DRIVER=$(adapter_driver "$ADAPTER")
    if [[ -z "$WANT_DRIVER" ]]; then
        fail "unknown adapter profile '$ADAPTER'; set [host].adapter to mt7601u or tplink-archer-t3u"
    else
        SELECTED_PHY=$(find_phy_by_driver "$WANT_DRIVER" || true)
        if [[ -z "$SELECTED_PHY" ]]; then
            fail "no phy found for adapter '$ADAPTER' ($WANT_DRIVER driver); is the dongle attached?"
        else
            pass "adapter '$ADAPTER' resolved to $SELECTED_PHY ($WANT_DRIVER driver)"
        fi
        # The TP-Link is additionally identified by its exact USB id.
        if [[ "$ADAPTER" == "tplink-archer-t3u" ]]; then
            if command -v lsusb >/dev/null 2>&1 && lsusb -d 2357:012d >/dev/null; then
                pass "TP-Link USB 2357:012d is attached"
            else
                fail "TP-Link Archer T3U (USB 2357:012d) is not attached"
            fi
        fi
    fi
fi

if [[ -n "$SELECTED_PHY" ]]; then
    validate_selected_phy "$SELECTED_PHY"
fi

if [[ -n "$KEYS_PATH" ]]; then
    if [[ "$CONFIG_KEYS_PATH" != /* ]]; then
        fail "[ldn].keys_path must be an absolute Pi path; set it in config/host.local.toml"
    elif [[ ! -f "$KEYS_PATH" ]]; then
        fail "Switch keys are missing (install with scripts/install_switch_keys.sh)"
    elif [[ $(stat -c '%a' "$KEYS_PATH") != "600" ]]; then
        fail "Switch keys must have mode 600"
    else
        pass "Switch keys are installed with mode 600"
    fi
fi

NM_CONF=/etc/NetworkManager/conf.d/zz-frlg-ldn-unmanaged.conf
if [[ -r "$NM_CONF" ]] && grep -q 'interface-name:ldn-mon' "$NM_CONF" \
        && grep -q 'interface-name:ldn-tap' "$NM_CONF"; then
    pass "NetworkManager ignores LDN-created interfaces"
else
    fail "NetworkManager LDN exclusion is missing; run scripts/setup_pi.sh"
fi

if ((FAILURES)); then
    printf '\nPreflight failed (%d check(s)). Fix the items above before hosting.\n' "$FAILURES" >&2
    exit 1
fi
printf '\nPi preflight passed. Start with scripts/run_mystery_gift.sh\n'
