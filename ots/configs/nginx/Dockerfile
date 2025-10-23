# syntax=docker/dockerfile:1
# ************************************************************
# First stage: builder
# ************************************************************
FROM python:3.12-slim as build

WORKDIR /build

RUN pip3 install lastversion
RUN lastversion --assets extract brian7704/OpenTAKServer-UI

# ************************************************************
# Second stage: runtime
# ************************************************************
FROM nginx:mainline-bookworm

# Copy nginx settings
COPY --chmod=0755 ./ots/configs/nginx/docker-entrypoint.d/10-default-ots.envsh /docker-entrypoint.d
COPY --from=build /build /usr/share/nginx/html/
COPY ./ots/configs/nginx/includes/ /etc/nginx/includes.d

HEALTHCHECK --interval=30s --start-period=30s CMD curl -k -I -A 'Docker-healthcheck' --fail http://localhost/health || exit 1

EXPOSE 80
