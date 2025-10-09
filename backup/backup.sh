#!/bin/bash
export RESTIC_PASSWORD_FILE=/home/soundscape/.secrets/restic_password
export RESTIC_REPOSITORY='sftp://u497612@u497612.your-storagebox.de:23/restic'

# backup the activities and media files for the authoring tool
restic backup /home/soundscape/share.soundscape.services

# backup the authoring tool database
restic backup --stdin-filename=authoring-tool-production.sql \
  --stdin-from-command -- \
  docker compose -f ~/authoring-tool2/docker-compose.yml  exec postgres pg_dump -U postgres postgres

  