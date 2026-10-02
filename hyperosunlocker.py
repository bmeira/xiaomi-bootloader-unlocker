import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import ntplib
import pytz
import urllib3
from colorama import Fore, Style, init
from urllib3.util import Retry

init(autoreset=True)

import io
import atexit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _TeeLogger:
    """Write to the real stream AND accumulate a plain-text copy for the log file."""

    def __init__(self, real_stream):
        self._real = real_stream
        self._buf = io.StringIO()

    def write(self, data):
        self._real.write(data)
        self._buf.write(_ANSI_RE.sub("", data))

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._real, "errors", "replace")

    def isatty(self):
        return False

    def get_log(self):
        return self._buf.getvalue()


_tee_stdout = _TeeLogger(sys.stdout)
_tee_stderr = _TeeLogger(sys.stderr)
sys.stdout = _tee_stdout
sys.stderr = _tee_stderr
_SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))


def _write_log_file():
    import datetime as _dt
    date_str = _dt.datetime.now().strftime("%d-%m-%Y")
    log_path = os.path.join(_SCRIPT_DIR, f"{date_str}.log")
    combined = _tee_stdout.get_log() + _tee_stderr.get_log()
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(combined)
        # Write directly to the real stream (stdout already replaced)
        _tee_stdout._real.write(f"\n[log]: Run saved to {log_path}\n")
        _tee_stdout._real.flush()
    except Exception as exc:
        _tee_stdout._real.write(f"\n[log error]: could not write log: {exc}\n")
        _tee_stdout._real.flush()


atexit.register(_write_log_file)


col_g = Fore.GREEN
col_b = Fore.BLUE
col_y = Fore.YELLOW
col_c = Fore.CYAN
col_m = Fore.MAGENTA
col_r = Fore.RED

DEFAULT_NTP_SERVERS = [
    "time.cloudflare.com",
    "pool.ntp.org",
    "time.google.com",
    "time.apple.com",
    "0.pool.ntp.org",
    "1.pool.ntp.org",
    "ntp.aliyun.com",
]


def env_or(name, default):
    return os.getenv(name, default)


def env_csv(name, default_items):
    raw = os.getenv(name, "")
    if not raw.strip():
        return list(default_items)
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_args():
    parser = argparse.ArgumentParser(description="hyperos bootloader quota runner (optimized multi-threaded)")
    parser.add_argument("--token", default=env_or("HYPEROS_TOKEN", ""), help="new_bbs_serviceToken or full cookie string")
    parser.add_argument("--version-code", default=env_or("HYPEROS_VERSION_CODE", "500411"), help="versionCode cookie value")
    parser.add_argument("--version-name", default=env_or("HYPEROS_VERSION_NAME", "5.4.11"), help="versionName cookie value")
    parser.add_argument(
        "--phase-ms",
        type=float,
        default=float(env_or("HYPEROS_PHASE_MS", "2000")),
        help="ms before bj midnight to fire the FIRST request (default: 2000ms); RTT-adapted at T-30s)",
    )
    parser.add_argument(
        "--burst-count",
        type=int,
        default=int(env_or("HYPEROS_BURST_COUNT", "25")),
        help="number of requests to spread across the pre-midnight window (default: 20; at 100ms gap covers ~2s window)",
    )
    parser.add_argument(
        "--burst-gap-ms",
        type=float,
        default=float(env_or("HYPEROS_BURST_GAP_MS", "100")),
        help="stagger interval between requests in ms (default: 100ms; spreads requests across a 2s pre-midnight window)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(env_or("HYPEROS_WORKERS", "15")),
        help="number of concurrent worker threads (default: 15)",
    )
    parser.add_argument(
        "--status-url",
        default=env_or("HYPEROS_STATUS_URL", "https://sgp-api.buy.mi.com/bbs/api/global/user/bl-switch/state"),
        help="unlock status endpoint",
    )
    parser.add_argument(
        "--apply-url",
        default=env_or("HYPEROS_APPLY_URL", "https://sgp-api.buy.mi.com/bbs/api/global/apply/bl-auth"),
        help="unlock apply endpoint",
    )
    parser.add_argument(
        "--user-agent",
        default=env_or("HYPEROS_USER_AGENT", "okhttp/4.12.0"),
        help="request user-agent",
    )
    parser.add_argument(
        "--ntp-servers",
        default=",".join(env_csv("HYPEROS_NTP_SERVERS", DEFAULT_NTP_SERVERS)),
        help="comma list of ntp servers",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        default=os.getenv("HYPEROS_SKIP_CHECK", "").lower() in ("1", "true", "yes"),
        help="skip initial account unlock status check",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=os.getenv("HYPEROS_ASSUME_YES", "").lower() in ("1", "true", "yes"),
        help="automatically answer yes to interactive prompts",
    )
    parser.add_argument(
        "--test-run",
        action="store_true",
        default=os.getenv("HYPEROS_TEST_RUN", "").lower() in ("1", "true", "yes"),
        help="dry-run test: schedule target 5 seconds from now and fire 3 test requests to verify full pipeline",
    )
    return parser.parse_args()


def generate_device_id():
    random_data = f"{random.random()}-{time.time()}"
    return hashlib.sha1(random_data.encode("utf-8")).hexdigest().upper()


def extract_service_token(raw_input):
    token = raw_input.strip()
    if not token:
        return ""
    match = re.search(r"new_bbs_serviceToken=([^;]+)", token)
    if match:
        return match.group(1).strip()
    match_st = re.search(r"(?:^|;\s*)serviceToken=([^;]+)", token)
    if match_st:
        return match_st.group(1).strip()
    return token


def resolve_token(cli_token):
    token = extract_service_token(cli_token)
    if token:
        return token
    print(col_y + "Paste new_bbs_serviceToken value (or full cookie string):" + Fore.RESET)
    try:
        token_input = input("token: ")
    except EOFError:
        raise SystemExit("[error] no token provided via stdin or flag")
    token = extract_service_token(token_input)
    if not token:
        raise SystemExit("empty token input")
    return token


def build_cookie_header(service_token, device_id, version_code, version_name):
    return (
        f"new_bbs_serviceToken={service_token};"
        f"versionCode={version_code};"
        f"versionName={version_name};"
        f"deviceId={device_id};"
    )


def get_initial_beijing_time(ntp_servers):
    client = ntplib.NTPClient()
    beijing_tz = pytz.timezone("Asia/Shanghai")
    print(col_y + "\n[NTP] Synchronizing high-precision time..." + Fore.RESET)
    for server in ntp_servers:
        try:
            response = client.request(server, version=3, timeout=3)
            ntp_time = datetime.fromtimestamp(response.tx_time, timezone.utc)
            beijing_time = ntp_time.astimezone(beijing_tz)
            local_time = beijing_time.astimezone()
            print(col_g + "[ntp server]: " + Fore.RESET + server)
            print(col_g + "[bj time]:    " + Fore.RESET + f"{beijing_time.strftime('%Y-%m-%d %H:%M:%S.%f')} (UTC+8)")
            print(col_g + "[local time]: " + Fore.RESET + f"{local_time.strftime('%Y-%m-%d %H:%M:%S.%f')} ({local_time.tzname() or 'local'})")
            return beijing_time
        except Exception as exc:
            print(f"[ntp fail] {server}: {exc}")
    return None


def synced_beijing_time(start_beijing_time, start_tick):
    elapsed = time.perf_counter() - start_tick
    return start_beijing_time + timedelta(seconds=elapsed)


class FastHttpSession:
    def __init__(self, user_agent, pool_size=20):
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=2,
            backoff_factor=0.05,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        self.base_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": user_agent,
            "Connection": "keep-alive",
        }
        self.http = urllib3.PoolManager(
            maxsize=pool_size,
            retries=retries,
            timeout=urllib3.Timeout(connect=2.5, read=10.0),
        )

    def request(self, method, url, headers=None, body=None):
        request_headers = dict(self.base_headers)
        if headers:
            request_headers.update(headers)
        if method == "POST" and body is None:
            body = b'{"is_retry":true}'
        try:
            return self.http.request(
                method,
                url,
                headers=request_headers,
                body=body,
                preload_content=True,
            )
        except Exception:
            return None


def parse_json_response(response):
    if response is None:
        return None
    try:
        return json.loads(response.data.decode("utf-8"))
    except Exception:
        return None


def wants_continue(prompt, assume_yes=False):
    if assume_yes:
        return True
    try:
        answer = input(prompt).strip().lower()
        return answer in {"y", "yes"}
    except EOFError:
        return False


def check_unlock_status(session, status_url, cookie_header, assume_yes=False):
    response = session.request("GET", status_url, headers={"Cookie": cookie_header})
    payload = parse_json_response(response)
    if payload is None:
        print(col_r + "[error] failed to fetch account status." + Fore.RESET)
        return False

    if payload.get("code") == 100004:
        raise SystemExit(col_r + "[error] Cookie/token expired or invalid! Please obtain a fresh token." + Fore.RESET)

    data = payload.get("data") or {}
    is_pass = data.get("is_pass")
    button_state = data.get("button_state")
    deadline = data.get("deadline_format", "")

    if is_pass == 4 and button_state == 1:
        print(col_g + "[account]: " + Fore.RESET + "Ready! Account eligible to apply.")
        return True
    if is_pass == 4 and button_state == 2:
        print(col_y + "[account]: " + Fore.RESET + f"Requests blocked until {deadline}.")
        return wants_continue(f"Continue anyway ({col_b}yes/no{Fore.RESET})? ", assume_yes)
    if is_pass == 4 and button_state == 3:
        print(col_y + "[account]: " + Fore.RESET + "Account is younger than 30 days.")
        return wants_continue(f"Continue anyway ({col_b}yes/no{Fore.RESET})? ", assume_yes)
    if is_pass == 1:
        raise SystemExit(col_g + "[account]: " + Fore.RESET + f"Already APPROVED until {deadline}!")

    print(col_y + "[account]: " + Fore.RESET + f"State: is_pass={is_pass}, button_state={button_state}, deadline={deadline}")
    return True


def warm_up_connections(session, test_url, cookie_header, num_pings=3):
    rtts = []
    for _ in range(num_pings):
        t0 = time.perf_counter()
        session.request("GET", test_url, headers={"Cookie": cookie_header})
        rtt_ms = (time.perf_counter() - t0) * 1000.0
        rtts.append(rtt_ms)
        time.sleep(0.05)
    return min(rtts)


def wait_until_target_time(session, status_url, cookie_header, start_beijing_time, start_tick, phase_ms, burst_count=25, test_run=False):
    measured_one_way_ms = None  # will be set when RTT is measured
    adapted_burst_gap_ms = None  # will be set when RTT is measured
    if test_run:
        target_time = start_beijing_time + timedelta(seconds=5)
    else:
        next_day = start_beijing_time + timedelta(days=1)
        target_time = next_day.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(milliseconds=phase_ms)

    target_local = target_time.astimezone()

    print(col_y + "\nBootloader unlock quota target scheduled:" + Fore.RESET)
    print(col_g + "[phase offset]:  " + Fore.RESET + f"{phase_ms:.2f} ms before trigger")
    print(col_g + "[target Beijing]:" + Fore.RESET + f" {target_time.strftime('%Y-%m-%d %H:%M:%S.%f')} (UTC+8)")
    print(col_g + "[target local]:  " + Fore.RESET + f" {target_local.strftime('%Y-%m-%d %H:%M:%S.%f')} ({target_local.tzname() or 'local'})")
    print("Keep this terminal open. Countdown in progress...\n")

    last_log_sec = None
    warmed_up_30s = False
    warmed_up_5s = False
    warmed_up_2s = False

    while True:
        now = synced_beijing_time(start_beijing_time, start_tick)
        remaining = (target_time - now).total_seconds()
        if remaining <= 0:
            print(col_c + f"\n[TRIGGER HIT]: {now.strftime('%Y-%m-%d %H:%M:%S.%f')} (Beijing) -> Dispatching burst!" + Fore.RESET)
            return measured_one_way_ms, adapted_burst_gap_ms

        current_sec = int(remaining)

        if not test_run:
            if remaining <= 30.0 and not warmed_up_30s:
                warmed_up_30s = True
                print(col_m + "[warmup]: Measuring network latency to Xiaomi API..." + Fore.RESET)
                measured_rtt = warm_up_connections(session, status_url, cookie_header, num_pings=5)
                one_way_ms = measured_rtt / 2.0
                safety_buffer_ms = 2000.0  # aim to arrive 2s before midnight; burst spreads to midnight
                adapted_phase_ms = one_way_ms + safety_buffer_ms
                print(col_m + f"[warmup]: Roundtrip latency: {measured_rtt:.1f}ms (one-way flight ~{one_way_ms:.1f}ms)" + Fore.RESET)

                # Re-anchor target_time based on live measurement so requests
                # arrive at the server at midnight, not depart from here at midnight.
                midnight = (start_beijing_time + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                old_phase_ms = (midnight - target_time).total_seconds() * 1000
                target_time = midnight - timedelta(milliseconds=adapted_phase_ms)
                target_local = target_time.astimezone()
                measured_one_way_ms = one_way_ms  # expose to caller
                # Distribute burst_count requests evenly across the window:
                # first arrives safety_buffer_ms before midnight, last AT midnight.
                adapted_burst_gap_ms = safety_buffer_ms / max(1, burst_count - 1)
                print(
                    col_m
                    + f"[warmup]: Adapted phase offset: {old_phase_ms:.0f}ms → {adapted_phase_ms:.1f}ms "
                    + f"(one-way {one_way_ms:.1f}ms + {safety_buffer_ms:.0f}ms pre-midnight window)"
                    + Fore.RESET
                )
                print(
                    col_m
                    + f"[warmup]: Burst gap adapted: {adapted_burst_gap_ms:.1f}ms "
                    + f"({burst_count} requests from 2s before midnight → 00:00:00.000 server arrival)"
                    + Fore.RESET
                )
                print(
                    col_m
                    + f"[warmup]: New target: {target_time.strftime("%Y-%m-%d %H:%M:%S.%f")} (UTC+8)"
                    + f" / {target_local.strftime("%H:%M:%S.%f")} ({target_local.tzname() or "local"})"
                    + Fore.RESET
                )

            if remaining <= 6.0 and not warmed_up_5s:
                warmed_up_5s = True
                print(col_m + "[warmup]: Priming connection pool sockets (T - 5s)..." + Fore.RESET)
                session.request("GET", status_url, headers={"Cookie": cookie_header})

            if remaining <= 2.0 and not warmed_up_2s:
                warmed_up_2s = True
                print(col_m + "[warmup]: Final socket keep-alive ping (T - 2s)..." + Fore.RESET)
                session.request("GET", status_url, headers={"Cookie": cookie_header})

        if remaining > 60:
            if last_log_sec is None or (last_log_sec - current_sec) >= 30:
                mins, secs = divmod(current_sec, 60)
                print(f"[countdown]: {mins:02d}m {secs:02d}s remaining (current BJ time: {now.strftime('%H:%M:%S')})")
                last_log_sec = current_sec
            time.sleep(1.0)
        elif remaining > 10:
            if last_log_sec is None or (last_log_sec - current_sec) >= 5:
                print(f"[countdown]: {remaining:.1f}s remaining...")
                last_log_sec = current_sec
            time.sleep(0.5)
        elif remaining > 2:
            if last_log_sec is None or (last_log_sec - current_sec) >= 1:
                print(f"[countdown]: {remaining:.2f}s...")
                last_log_sec = current_sec
            time.sleep(0.05)
        elif remaining > 0.05:
            time.sleep(0.005)
        else:
            time.sleep(0.0005)


def run_concurrent_burst(session, apply_url, status_url, cookie_header, start_beijing_time, start_tick, burst_count, burst_gap_ms, workers, one_way_ms=None):
    stagger_sec = max(0.001, burst_gap_ms / 1000.0)
    print(col_y + f"Firing {burst_count} concurrent burst requests (stagger: {burst_gap_ms}ms, workers: {workers})...\n" + Fore.RESET)

    approved_event = Event()
    print_lock = Lock()
    results = []

    def send_one(req_id):
        if approved_event.is_set():
            return
        try:
            t_send = synced_beijing_time(start_beijing_time, start_tick)
            with print_lock:
                if one_way_ms is not None:
                    t_est_arrival = t_send + timedelta(milliseconds=one_way_ms)
                    print(col_g + f"[request #{req_id:02d}]:" + Fore.RESET
                          + f" sent {t_send.strftime('%H:%M:%S.%f')[:-3]} | est. arrival {t_est_arrival.strftime('%H:%M:%S.%f')[:-3]} (UTC+8)")
                else:
                    print(col_g + f"[request #{req_id:02d}]:" + Fore.RESET + f" sent at {t_send.strftime('%H:%M:%S.%f')[:-3]} (UTC+8)")

            resp = session.request("POST", apply_url, headers={"Cookie": cookie_header})
            t_recv = synced_beijing_time(start_beijing_time, start_tick)
            payload = parse_json_response(resp)

            if payload is None:
                with print_lock:
                    print(col_r + f"[response #{req_id:02d}]:" + Fore.RESET + f" network timeout or empty response ({t_recv.strftime('%H:%M:%S.%f')[:-3]})")
                return

            code = payload.get("code")
            data = payload.get("data") or {}
            apply_result = data.get("apply_result")
            deadline = data.get("deadline_format", "")

            with print_lock:
                results.append((req_id, code, apply_result, payload))
                if code == 0 and apply_result == 1:
                    print(Style.BRIGHT + Fore.GREEN + f"\n🎉 [SUCCESS #{req_id:02d}]: UNLOCK REQUEST APPROVED! (received at {t_recv.strftime('%H:%M:%S.%f')[:-3]})" + Style.RESET_ALL)
                    approved_event.set()
                elif code == 0 and apply_result == 3:
                    print(col_y + f"[response #{req_id:02d}]:" + Fore.RESET + f" Quota reached (recvd: {t_recv.strftime('%H:%M:%S.%f')[:-3]}). Try at {deadline}")
                elif code == 0 and apply_result == 4:
                    print(col_r + f"[response #{req_id:02d}]:" + Fore.RESET + f" Account blocked until {deadline}")
                else:
                    msg = payload.get("msg", "")
                    print(col_b + f"[response #{req_id:02d}]:" + Fore.RESET + f" code={code} ({msg}) at {t_recv.strftime('%H:%M:%S.%f')[:-3]}")
        except Exception as exc:
            with print_lock:
                print(col_r + f"[error #{req_id:02d}]: {exc}" + Fore.RESET)

    executor = ThreadPoolExecutor(max_workers=workers)
    futures = []
    for i in range(1, burst_count + 1):
        if approved_event.is_set():
            break
        futures.append(executor.submit(send_one, i))
        time.sleep(stagger_sec)

    wait(futures)
    executor.shutdown(wait=True)

    if approved_event.is_set():
        print(Style.BRIGHT + col_g + "\n=== VERIFYING FINAL UNLOCK STATUS ===" + Style.RESET_ALL)
        check_unlock_status(session, status_url, cookie_header, assume_yes=True)
    else:
        print(col_y + "\nBurst completed." + Fore.RESET)


def main():
    args = parse_args()
    ntp_servers = [item.strip() for item in args.ntp_servers.split(",") if item.strip()]
    token = resolve_token(args.token)
    device_id = generate_device_id()
    cookie_header = build_cookie_header(token, device_id, args.version_code, args.version_name)
    session = FastHttpSession(args.user_agent, pool_size=args.workers + 5)

    if not args.skip_check:
        print(col_y + "Checking initial account status..." + Fore.RESET)
        if not check_unlock_status(session, args.status_url, cookie_header, args.yes):
            raise SystemExit(1)
    else:
        print(col_y + "Skipping initial account status check..." + Fore.RESET)

    start_beijing_time = get_initial_beijing_time(ntp_servers)
    if start_beijing_time is None:
        raise SystemExit(col_r + "[error] failed to fetch time via NTP." + Fore.RESET)
    start_tick = time.perf_counter()

    burst_count = 3 if args.test_run else args.burst_count
    measured_one_way_ms, adapted_burst_gap_ms = wait_until_target_time(
        session, args.status_url, cookie_header, start_beijing_time, start_tick,
        args.phase_ms, burst_count=burst_count, test_run=args.test_run
    )

    run_concurrent_burst(
        session=session,
        apply_url=args.apply_url,
        status_url=args.status_url,
        cookie_header=cookie_header,
        start_beijing_time=start_beijing_time,
        start_tick=start_tick,
        burst_count=burst_count,
        burst_gap_ms=adapted_burst_gap_ms if adapted_burst_gap_ms is not None else args.burst_gap_ms,
        workers=args.workers,
        one_way_ms=measured_one_way_ms,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Aborted by user]")
        sys.exit(0)
