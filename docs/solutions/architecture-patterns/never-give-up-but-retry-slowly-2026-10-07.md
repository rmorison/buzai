---
title: An always-on unit should never give up, but should retry slowly
date: 2026-10-07
category: architecture-patterns
module: deploy / systemd restart policy
problem_type: architecture_pattern
component: assistant
severity: high
related_components:
  - deployment
applies_when:
  - Choosing StartLimitIntervalSec / StartLimitBurst / RestartSec for an always-on --user unit
  - A service whose most common start failure is an expired or lapsed credential
  - Deciding which mechanism should alert on a long outage
tags:
  - systemd
  - start-limit
  - restart
  - always-on
  - credentials
  - observability
---

# An always-on unit should never give up, but should retry slowly

## Context

The assistant runs as a `--user` unit with `Restart=always`. It shipped with:

```ini
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Restart=always
RestartSec=10s
```

The start limit was there to make a hot loop visible: an otherwise silent restart cycle
would end in a `failed` state that someone would notice.

The most common reason this service cannot start for minutes on end is an expired or
lapsed login. SETUP treats that as an expected steady state, not a fault. Under the start
limit it plays out like this: five failed starts in five minutes, systemd stops trying,
and the unit is `failed`. The owner renews the credential, and nothing happens. The unit
stays `failed` until someone runs `systemctl --user restart` by hand. A condition that
would have cleared on its own becomes a permanent outage. Nothing tells the owner either,
because a `failed` user unit on a headless box goes unseen.

That also undercut the preflight's fatal/warning split (see the related entry), which
exists so that problems which are not leaks never take the assistant off the air.

## Guidance

**Never give up. Retry slowly. Alert from somewhere else.** Each of these settings fixes
something the others would leave broken.

- **`StartLimitIntervalSec=0` under `[Unit]`, and no `StartLimitBurst`.** The unit retries
  forever, so the first start after the credential is renewed succeeds and the assistant
  recovers without anyone touching it. The directive belongs to `[Unit]`. Under
  `[Service]`, systemd logs `Unknown key name 'StartLimitIntervalSec' in section
  'Service', ignoring.` and the old default limit stays in force, so the test checks which
  section it is in, not just that the key exists. Leave `StartLimitBurst` out entirely: it
  does nothing while the interval is 0, but it invites someone to "restore" an interval
  later and bring the give-up back.
- **`RestartSec=60s`.** Removing the limit alone causes the opposite failure. At 10s, a
  login outage logs about 360 failed starts an hour, and the journal is unreadable just
  when someone needs to read it. At 60s it is at most 60 an hour, and recovery after a
  renewal is about a minute away.
- **The watchdog (#4) is the other half.** Retrying forever means a long outage is
  survived, not noticed. Seeing that the assistant has been down for an hour is a separate
  job with its own signal. A start limit that tries to do that job does it by causing the
  outage it was meant to report.

The costs we accept:

- **A hot loop no longer ends in `failed`.** A real one, such as a misconfiguration that
  exits immediately or a fatal preflight finding, cycles once a minute until it is fixed.
  The cycle is slow, so it is cheap. **It is not visible yet:** the watchdog (#4) is
  deferred and has not been built. Until it is, a unit that cannot start shows
  `activating (auto-restart)`, not `failed`, so `systemctl --user is-failed` reports
  nothing. Read `systemctl --user status` and the journal. Removing the start limit
  removed the only alarm the unit had, and no replacement exists yet.
- **Every retry runs the whole start chain.** That covers the privacy probe in
  `ExecStartPre` and, once the server is spawned, the chained backlog pushes in
  `ExecStartPost` (commits, then review notes). During a long outage that is one probe
  and up to two push attempts per cycle against the hub host. Every step is time-bounded. The old 10s cadence without a limit would have run
  the chain six times as often, and under the start limit it stopped after five tries.
  The pushes use the hub's own credential, not the login, so they can run during a login
  outage. Nobody has checked on a host whether they finish when the server exits within
  seconds. The journal records each push's outcome, so read that rather than assuming a
  backlog is draining or stuck.
- **A cycle is longer than 60s.** `RestartSec` counts from the end of a failed attempt,
  so each cycle is 60s plus the start checks. Those take seconds on a good day and longer
  when the hub remote is slow or unreachable. Nobody has measured how long; the hard
  ceiling is `TimeoutStartSec` (600s). "60 an hour" is a ceiling.
  Recovery after a renewal is a minute plus however long those checks take.
- **A one-off crash also waits 60s.** A healthy server that exits once is back after a
  minute, not 10s. A backoff that starts short and grows (`RestartSteps`,
  `RestartMaxDelaySec`) avoids that, but it needs systemd 254, and Ubuntu 22.04 ships
  249. Revisit once the oldest supported host has 254.

## Why this matters

On a service with nobody watching, a failure mode that needs a human to undo it is worse
than the original fault. Expiring credentials are routine. The restart policy has to let
the service recover from them, and alerting has to come from a mechanism that does not
take the service down.

## Examples

```ini
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=60s
```

Check a rendered unit before installing it. Render it into a scratch directory the way
`make service-install` does: `cp` the template, then `sed` the `%h/buzai` paths to a
checkout that exists, so the `ExecStart*` binaries resolve. Then run:

```bash
systemd-analyze --user verify /path/to/scratch/claude-remote.service
```

`scripts/tests/test_service_unit.py` (`TestTheUnitNeverGivesUpButRetriesSlowly`) checks
all three settings and the section each one is in.

## Related

- [A start-time gate needs a severity split](start-time-gate-severity-split-2026-09-18.md): the fatal/warning split whose purpose the start limit undercut. Its reasoning still holds: a failed start still keeps the assistant off the air until the check passes, just no longer permanently.
- [Running Claude Code always-on](running-claude-code-always-on.md): the "active but silently broken" cluster.
- Origin: issue #5. The watchdog is #4.
