# ops
Infrastructure to support the backend services for soundscape

## Telemetry

The [Vector telemetry project](telemetry/README.md) receives Soundscape
OTLP logs and converts them for EU Mixpanel. Its project token is read from an
ignored file at runtime. See its README for setup, tests and iOS configuration.

## Doco-CD

[Doco-CD](https://github.com/kimdre/doco-cd/tree/main/examples/self-updating)
manages its own Compose project and telemetry from this repository. It polls
`main` every five minutes and updates projects when their configuration changes.
This public repository needs no Git credentials. Provision telemetry secrets as
described in [its README](telemetry/README.md) before enabling its deployment.

Merge the configuration into `main` before bootstrapping. On a Docker host with
no existing Doco-CD installation, create the persistent secrets directory first,
then run this once:

```sh
install -d -m 700 /home/soundscape/.secrets
docker volume create doco-cd_data
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v doco-cd_data:/data \
  -v /home/soundscape/.secrets:/home/soundscape/.secrets:ro \
  -e SELF_UPDATE_ENABLED=true \
  -e POLL_CONFIG='- url: https://github.com/soundscape-community/ops.git
  reference: main' \
  ghcr.io/kimdre/doco-cd:0.125.1@sha256:38198abbda1b55da7a7f37a7924b2ccdcc0583bd24466b7c2103ef6cd2ffc828 \
  apply-self --bootstrap
```

The bootstrap exits after creating the managed Doco-CD container. Its persistent
state lives in `doco-cd_data`. Keep the bootstrap image above and the image in
`doco-cd/compose.yaml` in sync when updating versions.

View logs or stop Doco-CD using its project and service labels:

```sh
docker logs --follow $(docker ps -q \
  --filter label=com.docker.compose.project=doco-cd \
  --filter label=com.docker.compose.service=doco-cd)

docker stop $(docker ps -q \
  --filter label=com.docker.compose.project=doco-cd \
  --filter label=com.docker.compose.service=doco-cd)
```

Stopping Doco-CD preserves its data volume. To resume, use `docker start` with
the container ID returned by the same filters with `docker ps -aq`.

## Persistent host secrets

Keep production secrets outside deployment checkouts, under
`/home/soundscape/.secrets/<project>/`. Run provisioning commands as `soundscape`;
use mode `0700` for directories and `0600` for secret files. The file permissions
must also allow the consuming container's UID to read them. Doco-CD mounts this
directory read-only at the same absolute path used by the host Docker daemon.
Only explicitly granted secret files should be mounted into application containers.
Keep values out of Git and environment variables, and include the directory in a
secure, access-controlled backup.

Roll out secrets support in two stages. First merge and deploy the Doco-CD mount
and bootstrap changes, creating the host directory before deployment. Verify the
running Doco-CD container has `/home/soundscape/.secrets` mounted read-only using
`docker inspect`. Only then merge the telemetry deployment changes: deployments
can run concurrently, so enabling telemetry in the same rollout may race the
Doco-CD update. Do not squash these stages into one production rollout.
