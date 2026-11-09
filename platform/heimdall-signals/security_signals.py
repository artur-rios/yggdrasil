#!/usr/bin/env python3
"""Collect SECURITY_SIGNAL lines from the logs and e-mail whatever is new.

`SecurityMonitoringService` writes a warning-level line for every signal that crosses a threshold,
and until this script existed nothing read them. That gap was not cosmetic: **both breach
notification clocks -- LGPD Art. 48 and GDPR Art. 33 -- run from awareness**, and a signal nobody
receives has not been detected. See NFR-26, the Incident Response Document Sec. 6, and residual risk
R-08 in the DPIA.

Run it on a schedule. It reads the rolling log files, finds the lines it has not reported before, and
sends them to one address through Hermes, the internal email service. Hermes has no published port,
so the job has to run in a container on the edge network, where it answers as
hermes-api.<environment>:8080; the operations guide has the command.

Usage (from anywhere in the repository):

    python scripts/security_signals.py                     # report new signals by e-mail
    python scripts/security_signals.py --stdout            # print them instead of sending
    python scripts/security_signals.py --stdout --all      # ignore the watermark, print everything
    python scripts/security_signals.py --since 2026-09-01  # only signals at or after a date

Configuration, all environment variables:

    HEIMDALL_LOG_DIRECTORY          where the API writes log-*.json  (same variable the API reads)
    HEIMDALL_SIGNAL_ALERT_TO        the address alerts go to         (required unless --stdout)
    HEIMDALL_SIGNAL_STATE_FILE      the watermark  (default: <log directory>/.security-signals-state)
    HERMES_BASE_URL                 Hermes at its internal alias     (required unless --stdout)
    HEIMDALL_SIGNAL_HERMES_API_KEY  this job's own key for Hermes    (required unless --stdout)

Exit codes: 0 ran and delivered whatever it found, 1 signals were found and could NOT be delivered,
2 the collection itself could not run.

The non-zero exit on a delivery failure is the point of the exit codes. This script is the thing
that makes somebody aware; if it fails silently the failure is invisible in exactly the way the
signals themselves were. Whatever schedules it has to report a non-zero exit somewhere a person
looks, so a broken collector is itself noticed.

**Why this job has its own Hermes client.** Hermes counts messages per client, and a client over a
limit gets a 429 until the window ends, which for the daily limit means midnight UTC. Under Heimdall's
client, the flood of password resets that raises a signal could also use up the day and refuse the
alert about it. Its own client keeps its own count, sends as its own sender, and may only mail the
alert address. So it reads its own key and never falls back to the API's HERMES_API_KEY.

**One message per run.** Hermes refuses a body over 10,000 characters, so a long report keeps every
count, lists the newest signals that fit, and says how to print the rest. The run still consumes all
of them: they were reported, and the logs keep the detail.

**What gets sent.** Only the marker, the kind, the count, the timestamp and the actor's PublicId --
the fields the service logs. A PublicId is a pseudonym: it identifies an account to somebody holding
the database and to nobody else, which is what keeps this within the same processing already
described in the ROPA. Do not extend this to include e-mail addresses; NFR-22 keeps them out of the
logs in the first place, and LogSafeEmail exists for the cases that need a reference.

**The watermark is what makes this readable.** Without it every run would resend the whole history,
and an alert that repeats everything it has ever said is one that gets filtered into a folder within
a week. Signals repeat on every monitoring tick while a condition persists -- that is deliberate in
the service -- so a run reports each repetition once, and no more.
"""

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must match SecurityMonitoringService.Marker. There is a test in the .NET suite that fails if the
# constant moves, so this string cannot drift without something going red.
MARKER = "SECURITY_SIGNAL"

DEFAULT_LOG_DIRECTORY = "logs"
LOG_FILE_GLOB = "log-*.json"
STATE_FILE_NAME = ".security-signals-state"

HERMES_EMAILS_PATH = "/api/emails"

# Hermes answers 400 to a longer body (SendEmailRequestValidator.MaximumBodyLength).
HERMES_BODY_LIMIT = 10_000

# So that one malformed line, or a log full of odd kinds, cannot use up a whole message.
MAX_ENTRY_LENGTH = 1_000
MAX_SUMMARY_LENGTH = 2_000

# Serilog writes seven fractional digits; datetime.fromisoformat accepts three or six and rejects
# the rest, so the tail is trimmed rather than the whole line being dropped as unparseable.
_FRACTION = re.compile(r"(\.\d{6})\d+")


class CollectionError(Exception):
    """The collection could not run -- a missing directory, an unreadable state file."""


class Signal:
    """One signal line, as it will be reported."""

    def __init__(self, timestamp, kind, description, count, actor, raw):
        self.timestamp = timestamp
        self.kind = kind
        self.description = description
        self.count = count
        self.actor = actor
        self.raw = raw

    @property
    def digest(self):
        """Identifies this line within its own timestamp, for the watermark's tie-breaking."""
        return sha256(self.raw.encode("utf-8", "replace")).hexdigest()[:16]

    def __repr__(self):
        stamp = self.timestamp.isoformat() if self.timestamp else "(no timestamp)"
        return f"{stamp}  {self.kind}  count={self.count}  actor={self.actor}"


def parse_timestamp(value):
    """Read one Serilog timestamp. Returns None rather than raising: a line with an unreadable
    timestamp is still a signal worth reporting, and losing it would be the wrong trade."""
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", value.strip()))
    except (ValueError, AttributeError):
        return None

    # A naive timestamp is treated as UTC so that every comparison against the watermark is between
    # two aware values. Mixing the two raises, and it would raise inside the alerting.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_line(line):
    """Turn one log line into a Signal, or None if it carries no signal.

    Handles both shapes deliberately. The API writes Serilog's JsonFormatter, so the properties are
    structured and are read as such -- matching the prose would break the moment somebody improves
    the wording. But a line that is not JSON at all, from a console capture or a future sink, still
    reports if it contains the marker. Dropping it would mean a formatting change could silently
    switch detection off."""
    if MARKER not in line:
        return None

    raw = line.strip()

    try:
        entry = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return Signal(None, "(unparsed)", raw, None, None, raw)

    if not isinstance(entry, dict):
        return Signal(None, "(unparsed)", raw, None, None, raw)

    properties = entry.get("Properties") or {}

    if not isinstance(properties, dict):
        properties = {}

    # Confirm the marker is the logged property rather than a substring of somebody's message.
    # Without this a user-supplied value containing the marker could raise a false alert.
    if properties.get("Marker") != MARKER:
        return None

    return Signal(
        parse_timestamp(entry.get("Timestamp")),
        properties.get("Kind") or "(no kind)",
        properties.get("Description") or "",
        properties.get("Count"),
        properties.get("ActorId"),
        raw,
    )


def read_state(path):
    """Return (watermark, digests-seen-at-that-instant). Missing or corrupt state means a first
    run: report everything rather than nothing, because an unreadable watermark must not be a
    silent way to stop alerting."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, set()
    except (OSError, json.JSONDecodeError):
        return None, set()

    if not isinstance(state, dict):
        return None, set()

    return parse_timestamp(state.get("watermark")), set(state.get("digests") or [])


def write_state(path, watermark, digests):
    payload = {
        "watermark": watermark.isoformat() if watermark else None,
        "digests": sorted(digests),
        "written": datetime.now(timezone.utc).isoformat(),
    }

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Written to a temporary file and moved, so an interrupted run cannot leave a truncated
    # watermark behind -- which would resend the whole history on the next run.
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(destination)


def advance(signals, watermark, digests):
    """Return the watermark and tie-breaking digests after reporting these signals.

    Two signals can share a timestamp -- the monitoring pass raises its findings in one tick. A
    watermark alone would then either resend one of them forever or drop it, so the digests of every
    signal at the newest instant are carried alongside it."""
    stamped = [signal for signal in signals if signal.timestamp]

    if not stamped:
        return watermark, digests

    newest = max(signal.timestamp for signal in stamped)

    if watermark and newest < watermark:
        return watermark, digests

    at_newest = {signal.digest for signal in stamped if signal.timestamp == newest}

    if watermark and newest == watermark:
        return watermark, digests | at_newest

    return newest, at_newest


def is_new(signal, watermark, digests):
    """A signal with no readable timestamp always reports: it cannot be placed against the
    watermark, and reporting it twice is a smaller failure than never reporting it."""
    if signal.timestamp is None:
        return True

    if watermark is None:
        return True

    if signal.timestamp > watermark:
        return True

    return signal.timestamp == watermark and signal.digest not in digests


def collect(log_directory, watermark=None, digests=frozenset(), since=None):
    """Read every log file and return the signals that have not been reported."""
    directory = Path(log_directory)

    if not directory.is_dir():
        raise CollectionError(
            f"No log directory at {directory}. Set HEIMDALL_LOG_DIRECTORY to where the API writes."
        )

    found = []

    for path in sorted(directory.glob(LOG_FILE_GLOB)):
        try:
            # errors="replace" because a partially written line at the tail of the file the API is
            # currently appending to must not abort the whole run.
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    signal = parse_line(line)

                    if signal is None:
                        continue

                    if since and signal.timestamp and signal.timestamp < since:
                        continue

                    if not is_new(signal, watermark, digests):
                        continue

                    found.append(signal)
        except OSError as error:
            raise CollectionError(f"Could not read {path}: {error}") from error

    found.sort(key=lambda signal: (signal.timestamp is not None, signal.timestamp or datetime.min.replace(tzinfo=timezone.utc)))

    return found


def text_length(text):
    """Length as Hermes measures it: C#'s string.Length counts UTF-16 code units, so a character
    outside the basic plane is two. Counting Python's way could send a body Hermes refuses."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clip(text, limit):
    if text_length(text) <= limit:
        return text

    mark = "..."
    units = text.encode("utf-16-le", "surrogatepass")[:(limit - len(mark)) * 2]

    # "ignore" drops the half of a surrogate pair the cut may have split.
    return units.decode("utf-16-le", "ignore") + mark


def render_entry(signal):
    stamp = signal.timestamp.isoformat() if signal.timestamp else "(no timestamp)"
    lines = [f"  {stamp}  {signal.kind}"]

    if signal.count is not None:
        lines.append(f"      count: {signal.count}")

    if signal.actor:
        lines.append(f"      actor: {signal.actor}")

    if signal.description:
        lines.append(f"      {signal.description}")

    return "\n".join(lines)


def omission_note(signals, omitted):
    """Says what was left out. The command ignores the watermark because delivering this message
    moves it past these signals, so a plain --stdout would find nothing."""
    oldest = next((signal.timestamp for signal in signals[:omitted] if signal.timestamp), None)
    since = f" --since {oldest.isoformat()}" if oldest else ""

    return (
        f"{omitted} older signal(s) are counted above but not listed, to keep this message within "
        f"the size Hermes accepts. To list them, run this job with: --stdout --all{since}"
    )


def fit_entries(signals, entries, fixed_lines, limit):
    """Choose the newest entries that fit, and the note about the rest. Signals arrive oldest first
    and the entries are taken from the end without gaps, so what is left out is always older than
    everything listed."""
    reserved = text_length(omission_note(signals, len(signals))) + 2
    budget = limit - text_length("\n".join(fixed_lines)) - reserved

    kept = 0

    for entry in reversed(entries):
        cost = text_length(entry) + 2

        if cost > budget:
            break

        budget -= cost
        kept += 1

    omitted = len(entries) - kept

    return entries[omitted:], omission_note(signals, omitted) if omitted else None


def describe(signals, limit=None):
    """The body of the alert. Written to be read at three in the morning: the counts first, then
    the lines, then what to do about it.

    With a limit the report fits it, keeping every count and as many of the newest lines as there is
    room for. Without one, as on stdout and stderr, nothing is left out."""
    if not signals:
        return "No new security signals."

    by_kind = {}

    for signal in signals:
        by_kind[signal.kind] = by_kind.get(signal.kind, 0) + 1

    summary = ", ".join(f"{kind} x{count}" for kind, count in sorted(by_kind.items()))

    if limit is not None:
        summary = clip(summary, MAX_SUMMARY_LENGTH)

    header = [
        f"{len(signals)} new security signal(s): {summary}",
        "",
        "A signal is not a breach. It is a reason to look.",
        "",
    ]

    footer = [
        "The actor is a PublicId, not a name or an address.",
        "",
        "What follows a signal is the Incident Response Document. If this turns out to be a",
        "breach, note the time you read this message: both notification clocks run from",
        "awareness, and this is when awareness happened.",
    ]

    entries = [render_entry(signal) for signal in signals]
    note = None

    if limit is not None:
        entries = [clip(entry, MAX_ENTRY_LENGTH) for entry in entries]
        entries, note = fit_entries(signals, entries, header + footer, limit)

    lines = list(header)

    for entry in entries:
        lines.extend([entry, ""])

    if note:
        lines.extend([note, ""])

    lines.extend(footer)

    return "\n".join(lines)


def hermes_emails_url(base):
    """Where to post, or an error naming the setting. Without this a value with no scheme ends the
    run in a ValueError instead of a delivery failure, and ftp:// would be followed."""
    try:
        parts = urllib.parse.urlsplit(base)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False

    if not valid:
        raise CollectionError("HERMES_BASE_URL must be an http or https address, such as http://hermes-api.production:8080.")

    return f"{base.rstrip('/')}{HERMES_EMAILS_PATH}"


def send(subject, body, recipient):
    """Send through Hermes with this job's own key. Raises on any failure -- the caller turns that
    into a non-zero exit, because a delivery failure here is indistinguishable in effect from having
    no detection at all. The one attempt is deliberate: the next scheduled run is the retry, and
    until then the watermark has not moved."""
    base = os.environ.get("HERMES_BASE_URL")
    key = os.environ.get("HEIMDALL_SIGNAL_HERMES_API_KEY")

    if not base or not key:
        raise CollectionError(
            "HERMES_BASE_URL and HEIMDALL_SIGNAL_HERMES_API_KEY must be set to send alerts. "
            "Use --stdout to print."
        )

    url = hermes_emails_url(base)
    payload = json.dumps({"to": recipient, "subject": subject, "body": body}).encode("utf-8")

    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            # 202 is the only answer that means Mailgun took the message.
            if response.status != 202:
                raise CollectionError(f"Hermes answered {response.status}, not 202")
    except urllib.error.HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", "replace")[:300]

        retry_after = error.headers.get("Retry-After") if error.headers else None
        retry = f", retry after {retry_after} seconds" if retry_after else ""
        raise CollectionError(f"Hermes refused the alert ({error.code}{retry}): {detail}") from error
    except (OSError, ValueError, http.client.HTTPException) as error:
        # Wider than URLError: a read timeout and a dropped connection are not wrapped in one, and
        # an uncaught one would end the run without printing the signals it was holding.
        raise CollectionError(f"Could not reach Hermes: {error}") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--log-directory",
        default=os.environ.get("HEIMDALL_LOG_DIRECTORY") or DEFAULT_LOG_DIRECTORY,
        help="where the API writes log-*.json (default: $HEIMDALL_LOG_DIRECTORY, else ./logs)")
    parser.add_argument(
        "--stdout", action="store_true",
        help="print the report instead of e-mailing it, and leave the watermark alone")
    parser.add_argument(
        "--all", action="store_true",
        help="ignore the watermark and report every signal in the logs")
    parser.add_argument(
        "--since", default=None,
        help="only report signals at or after this ISO-8601 instant or date")
    parser.add_argument(
        "--state-file", default=os.environ.get("HEIMDALL_SIGNAL_STATE_FILE"),
        help="the watermark (default: <log directory>/" + STATE_FILE_NAME + ")")
    parser.add_argument(
        "--quiet", action="store_true",
        help="say nothing when there were no new signals")

    arguments = parser.parse_args(argv)

    state_file = arguments.state_file or Path(arguments.log_directory) / STATE_FILE_NAME

    since = None

    if arguments.since:
        since = parse_timestamp(arguments.since)

        if since is None:
            print(f"Could not read --since {arguments.since!r} as a date.", file=sys.stderr)
            return 2

    watermark, digests = (None, set()) if arguments.all else read_state(state_file)

    try:
        signals = collect(arguments.log_directory, watermark, digests, since)
    except CollectionError as error:
        print(f"{error}", file=sys.stderr)
        return 2

    if not signals:
        if not arguments.quiet:
            print("No new security signals.")
        return 0

    body = describe(signals)
    subject = f"[Heimdall] {len(signals)} new security signal(s)"

    if arguments.stdout:
        print(body)
        return 0

    recipient = os.environ.get("HEIMDALL_SIGNAL_ALERT_TO")

    if not recipient:
        print(
            "HEIMDALL_SIGNAL_ALERT_TO is not set, so these signals have nowhere to go:",
            file=sys.stderr)
        print(body, file=sys.stderr)
        return 1

    try:
        send(subject, describe(signals, limit=HERMES_BODY_LIMIT), recipient)
    except CollectionError as error:
        # Printed in full on the way out. If the mail path is broken, stderr -- which cron mails, or
        # the journal keeps -- is the only place left that the signals can still reach somebody.
        print(f"{error}", file=sys.stderr)
        print(body, file=sys.stderr)
        return 1

    # Only after delivery succeeded. Advancing first would mean an outage or a refusal silently
    # consumed the signals it failed to send.
    write_state(state_file, *advance(signals, watermark, digests))

    if not arguments.quiet:
        print(f"Reported {len(signals)} signal(s) to {recipient}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
