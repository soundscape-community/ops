# ops
Infrastructure to support the backend services for soundscape

## Doco-CD

[Doco-CD](https://github.com/kimdre/doco-cd/tree/main/examples/self-updating)
manages its own Compose project from this repository. It polls `main` every five
minutes and updates itself when its configuration changes. No other services are
managed by Doco-CD yet. This public repository needs no Git credentials.

Merge the configuration into `main` before bootstrapping. On a Docker host with
no existing Doco-CD installation, run this once:

```sh
docker volume create doco-cd_data
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v doco-cd_data:/data \
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
