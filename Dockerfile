FROM python:3.13

RUN addgroup --gid 1024 ots
RUN adduser --home /app --disabled-password --gecos "" --force-badname --gid 1024 ots
RUN apt update && apt install ffmpeg -y

USER ots

WORKDIR /app/opentakserver

RUN chown -R ots:ots /app

RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Pinned to a tagged release. Unpinned git HEAD ships mid-development
# snapshots; 1.7.11 is the highest version with matching ghcr handler images.
RUN pip install opentakserver==1.7.11

# Fix two upstream 1.7.11 bugs that stop CoT reaching clients. See the script
# for details; it fails the build if either anchor is missing, so a version bump
# cannot silently drop a patch.
COPY --chown=ots:ots patches/fix_eud_handler.py /tmp/fix_eud_handler.py
RUN python3 /tmp/fix_eud_handler.py

RUN /app/venv/bin/flask --app /app/venv/lib/python3.13/site-packages/opentakserver/app.py ots create-ca
#RUN /app/venv/bin/flask --app /app/venv/lib/python3.13/site-packages/opentakserver/app.py db upgrade

EXPOSE 8081

ENTRYPOINT ["opentakserver"]

# Flask will stop gracefully on SIGINT (Ctrl-C).
# Docker compose tries to stop processes using SIGTERM by default, then sends SIGKILL after a delay if the process doesn't stop.
STOPSIGNAL SIGINT

HEALTHCHECK --interval=1m CMD curl --fail http://localhost:8081/api/health || exit 1