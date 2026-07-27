# OpenTAKServer-Docker

```
cp ots_config.env.template ots_config.env
```

`ots_config.env`:
```diff
+ OTS_FQDN=
- OTS_FQDN=<MacOS local network IP>

+ OTS_MEDIAMTX_TOKEN=<random token for mediamtx>
```

This repo contains the docker-compose.yml and config files needed to run OpenTAKServer in a docker container.
To use it, install docker on your system, clone this repo, and run `docker compose up -d` or `sudo docker compose up -d`
depending on your platform.

This repo is new and not quite complete. Full documentation will be available on https://docs.opentakserver.io when it's ready
for use. CloudTAK will also be incorporated into docker-compose.yml.
