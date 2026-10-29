# Soundscape telemetry

Vector receives the OTLP logs emitted by [Soundscape PR #326](https://github.com/soundscape-community/soundscape/pull/326)
and converts them to Mixpanel events. This standalone project targets the **EU**
Mixpanel project via `https://api-eu.mixpanel.com/import?strict=1`.
Production is managed by Doco-CD; local development uses Docker Compose directly.

## Run locally

Requires Docker with Compose. No Doco-CD installation or production host path is
needed. From this directory:

```sh
mkdir -p secrets
cp secrets/mixpanel-token.example secrets/mixpanel-token
chmod 600 secrets/mixpanel-token
# Edit secrets/mixpanel-token to contain only your project token.
docker compose config --quiet
docker compose run --rm --no-deps vector validate --no-environment /etc/vector/vector.yaml
docker compose up -d
docker compose logs --follow vector
```

By default, the token is read from `./secrets/mixpanel-token`. To use another
directory, optionally copy `.env.example` to `.env` and set `SECRETS_DIR` to a
relative or absolute directory path. Compose resolves relative paths against this
project directory. An exported `SECRETS_DIR` overrides `.env`. These settings
contain only a path, never the token itself. Both `.env` and the runtime token
are ignored by Git.

Compose mounts the selected token file read-only at
`/run/secrets/mixpanel-token`; Vector's directory secret backend reads it at
startup and removes trailing whitespace. Missing or empty secrets fail startup.
The token is used as the Basic Auth username, with an empty password. It is not
an environment variable and is not included in event properties. Never commit
the real token or include it in shell command arguments.

The pinned Vector 0.60.0 image uses a multi-platform manifest digest. Configuration
and VRL files are mounted read-only. HTTP is published only on
`127.0.0.1:4318`, with OTLP logs at `/v1/logs`; gRPC port 4317 is internal to the
container. Metrics and traces are outside this pipeline; Vector warns that these
source outputs have no consumers. Do not send them to this collector.

To replace the token, edit the ignored file and run:

```sh
docker compose up -d --force-recreate vector
```

Stop with `docker compose down`. The `vector-data` named volume retains the
disk buffer across container recreation. `docker compose down --volumes` deletes
it and all pending events. The buffer holds approximately 256 MiB and blocks when
full. HTTP delivery retries transient failures, including 429 and server errors;
delivery remains best effort. Permanent errors such as 400/401 can discard events.
With strict imports, Mixpanel can accept valid records in a partially invalid
batch while rejecting others. Check Vector logs for errors and dropped-event
counts; no raw-event console sink is configured.

## Production secrets and deployment

Follow the two-stage rollout in the [repository README](../README.md). Deploy the
Doco-CD secrets mount first and verify it is read-only before enabling telemetry.
The root deployment config sets project name `telemetry` and
`SECRETS_DIR=/home/soundscape/.secrets/telemetry`; no production `.env` is needed.
Doco-CD and the Docker daemon must see that same absolute path.

As `soundscape`, provision the directory and token file on the host:

```sh
install -d -m 700 /home/soundscape/.secrets
install -d -m 700 /home/soundscape/.secrets/telemetry
# For initial provisioning only; do not overwrite an existing token.
(umask 077; set -C; : > /home/soundscape/.secrets/telemetry/mixpanel-token)
# Securely edit the file to contain only the real project token.
chmod 600 /home/soundscape/.secrets/telemetry/mixpanel-token
```

Keep these files owned by `soundscape`. The pinned Vector image runs as root;
if changing to a non-root image/user, adjust file ownership or permissions for
its UID/GID. Do not rely on Compose secret `uid`, `gid` or `mode` settings to
change host-file permissions. Back up `.secrets` securely with restricted access.

Before enabling the telemetry deployment, validate the actual token is readable
with the pinned image. From this directory in a host checkout, run:

```sh
SECRETS_DIR=/home/soundscape/.secrets/telemetry \
docker compose -p telemetry run --rm --no-deps vector \
  validate --no-environment /etc/vector/vector.yaml
```

For takeover of a manually deployed instance, first inspect its container's
`com.docker.compose.project` label and the volume mounted at `/var/lib/vector`
with `docker inspect`. The expected project is `telemetry` and volume is
`telemetry_vector-data`. If they match, stop the old Vector container and let
Doco-CD recreate it with the same project and volume. Do not delete the volume.
If the old project name differs, stop the old project without `--volumes`, create
`telemetry_vector-data`, and copy the entire old buffer volume into it using a
temporary container with the old volume mounted read-only and the new volume
mounted read-write. Preserve ownership and permissions with `cp -a`. Keep the
old volume until successful delivery is confirmed. Do not enable the new
instance until the buffer is migrated and the old listener has stopped: only
one instance can bind port 4318.

For rotation, securely replace the production token file, then explicitly
redeploy and recreate Vector from a host checkout of the deployed revision:

```sh
SECRETS_DIR=/home/soundscape/.secrets/telemetry \
docker compose -p telemetry up -d --force-recreate vector
```

Editing a host file does not trigger a Git deployment or reload Vector's cached
secret. Recreation also remounts the file if replacement changed its inode.
Check Vector and Doco-CD logs after deployment; check for authentication failures
and dropped events. The named buffer volume survives recreation. Doco-CD retains
control of subsequent Git deployments.

## Event conversion

Vector uses native OTLP log decoding with legacy namespacing:

| OTLP / Vector field | Mixpanel field |
| --- | --- |
| `eventName` / `event_name` | `event` |
| resource attributes / `resources` | `properties` |
| event `attributes` | `properties`, overriding resource attributes |
| `attributes.user_id` | `properties.distinct_id` |
| occurrence timestamp | `properties.time`, integer Unix seconds |
| nonempty `attributes.event_id` | `properties.$insert_id` |
| `attributes.session_id` | `properties.session_id`, unchanged |

`event_id` also remains available as `properties.event_id`. Configure Mixpanel's
session definition to use the event property **`session_id`**. This identifies a
usage session; `distinct_id` continues to identify the client. Vector preserves
the app's session UUID on retries and replay and does not generate sessions for
older records that lack the attribute.

In the updated PR, each app process starts a new session. Backgrounding, phone
lock and ordinary inactivity keep it. Waking after at least 30 continuous minutes
in Soundscape sleep/snooze starts a new session; shorter sleeps keep the old ID.
Queued events retain the session captured at creation, even after a new session
starts.

Mixpanel's reserved identity, timestamp and insertion ID are set after merging
properties, overriding any caller-supplied values. Existing string properties,
including `value`, remain strings. Resource keys such as `service.version` and
`service.build` remain literal dotted property names. Missing or whitespace-only
event names and identities are dropped, with count diagnostics rather than raw
records. No anonymous identity is invented.

Without `event_id`, `$insert_id` is a SHA-256 hash of canonical JSON containing
the name, original timestamp in nanoseconds, resources and attributes. It excludes
observed/ingestion timestamps, so retry and replay preserve IDs. Two otherwise
identical occurrences with the same original timestamp will be merged. A UUID
created once by the app is the preferred solution.

## Verify

From the repository root, run:

```sh
python3 telemetry/tests/check.py
```

This uses only Docker with Compose and Python's standard library. It first checks
secret-path resolution for the default, an isolated `.env`, and the production
environment override without changing any existing local configuration. It validates Vector with a
dummy file secret, runs transform tests, and sends bodyless OTLP protobuf logs
through a temporary receiver to a local mock import endpoint. It checks a batch
of two events, Basic Auth, NDJSON, a forced 503 retry, stable event/session IDs
through replay, and compatibility with older records without session IDs.
It uses Linux Docker host networking and temporary containers, with dynamically
allocated ports; it does not start the normal Compose service or use its buffer.

To additionally send **one synthetic event to the EU project**, using the runtime
token file selected by Compose (including `.env` and exported `SECRETS_DIR`
overrides):

```sh
python3 telemetry/tests/check.py --live
```

The live test forwards Vector's import request through a local test relay to EU
Mixpanel, checks the real HTTP response reports one imported record, and prints
the insertion ID. Only the selected token file is mounted, read-only; missing or
empty files fail with a path-only error. The relay neither logs nor persists the
Authorization header.
In Mixpanel **Events → Live view**, look for `soundscape.telemetry_smoke_test`
with `distinct_id=ops-smoke-test` and the printed `$insert_id`. API acceptance is
automated; confirmation in Live view is a separate manual step.

## iOS follow-up for PR #326

- The updated PR generates `event_id` once per event and captures `session_id`
  at creation. Both are protected against caller/snapshot overrides and retained
  through persistence/replay. The collector maps the event UUID to `$insert_id`
  and forwards the session UUID as `session_id` without modification.
- The PR's collector build settings are empty. Configure
  `SOUNDSCAPE_OTEL_LOGS_ENDPOINT` as a reachable **HTTPS** URL ending in `/v1/logs`.
  The app rejects plain HTTP, so this local Compose listener alone is insufficient
  for device testing. Arrange HTTPS forwarding separately. The app documentation
  explains xcconfig URL escaping and optional headers. Never embed the Mixpanel
  token in the app.
- The PR already sends identity (`user_id`), event name (`eventName`) and original
  timestamp. No app-side Mixpanel SDK or event schema registration is needed.

Server ingress and Caddy configuration are deferred.

References: [Vector OTLP source](https://vector.dev/docs/reference/configuration/sources/opentelemetry/),
[Vector HTTP sink](https://vector.dev/docs/reference/configuration/sinks/http/),
[Vector secrets](https://vector.dev/docs/reference/configuration/secrets/),
[Mixpanel import API](https://docs.mixpanel.com/reference/import-events).
