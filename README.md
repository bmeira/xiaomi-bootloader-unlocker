# xiaomi-bootloader-unlocker

Simple script to send timed unlock apply requests around Beijing midnight quota reset (00:00:00 Asia/Shanghai, UTC+8).

## Credits

Request/session handling ideas inspired by community tooling:
- [offici5l/MiUnlockTool](https://github.com/offici5l/MiUnlockTool/tree/main/MiUnlockTool/src/miunlock)

---

## Prerequisites & Getting Token

1. **Xiaomi Community App (Global)**:
   - Account must be logged in and older than 30 days.
   - Region in Xiaomi Community app must be set to **Global** (`Me -> Set up -> Change region -> Global`).
   - Go to `Me -> Unlock bootloader -> Apply for unlock`.
2. **Extract `new_bbs_serviceToken`**:
   - Log into [Xiaomi Account / Community](https://account.xiaomi.com) in your browser.
   - Open Developer Tools (F12) -> Application / Storage -> Cookies.
   - Find and copy the value of `new_bbs_serviceToken` (or copy the entire cookie string).

---

## Running with Nix (Recommended / Ephemeral)

No persistent dependencies or `pip install` required!

### Option 1: Using the provided `shell.nix`

From the repository directory:

```bash
# Interactive token prompt
nix-shell --run "hyperosunlocker"

# Or provide token directly
nix-shell --run "hyperosunlocker --token 'your_new_bbs_serviceToken'"
```

When the Nix shell is active, run `hyperosunlocker` (or `hyperosunlocker --token ...`) from any directory.

### Option 2: One-liner from anywhere (pure ephemeral nix-shell)

```bash
nix-shell -p "python3.withPackages (ps: with ps; [ ntplib pytz urllib3 colorama ])" \
  --run "python hyperosunlocker.py --token 'your_new_bbs_serviceToken'"
```

---

## When to Run (Timezones & Portugal)

Xiaomi's server quota resets every day at **00:00:00 Beijing Time (UTC+8)**.

- **Portugal (Summer / WEST, UTC+1 - late March to late October)**:
  - Reset is at **17:00:00 (5:00 PM)** local time.
- **Portugal (Winter / WET, UTC+0 - late October to late March)**:
  - Reset is at **16:00:00 (4:00 PM)** local time.

**Recommendation**: Start the script **10 to 15 minutes before reset** (e.g. at 16:45 in summer or 15:45 in winter). It synchronizes its clock with NTP, measures the Xiaomi API round-trip latency shortly before midnight, then schedules requests so their *estimated server arrivals* span a two-second window centered on midnight. The estimate assumes symmetric network latency, so the actual arrival times can vary.

The default is 30 requests, dispatched at even intervals across that window. The script starts enough workers to avoid requests sitting in a local thread queue behind slower responses, and disables automatic POST retries so the configured burst count is also the maximum number of apply attempts. This improves the odds of requests reaching the service as the quota resets; it cannot guarantee a slot or observe the exact time the server receives each request.

---

## Useful Flags

- `--phase-ms 2000` : Initial countdown target before latency is measured; the target is then adjusted automatically (default: `2000`)
- `--burst-count 30` : Number of apply attempts spread across the arrival window (default: `30`, maximum: `30`)
- `--burst-gap-ms 100` : Fallback spacing if the latency measurement is unavailable (normally auto-calculated)
- `--workers 30` : Minimum worker count; raised automatically to the burst count if needed (maximum: `30`)
- `--skip-check` : Skip initial account unlock state check
- `-y`, `--yes` : Auto-confirm prompts

## Environment Variables

- `HYPEROS_TOKEN`
- `HYPEROS_PHASE_MS`
- `HYPEROS_BURST_COUNT`
- `HYPEROS_BURST_GAP_MS`
- `HYPEROS_WORKERS`
- `HYPEROS_STATUS_URL`
- `HYPEROS_APPLY_URL`
- `HYPEROS_USER_AGENT`
- `HYPEROS_VERSION_CODE`
- `HYPEROS_VERSION_NAME`
- `HYPEROS_NTP_SERVERS` (comma-separated)
