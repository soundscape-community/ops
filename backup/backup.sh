#!/bin/bash
export RESTIC_PASSWORD_FILE=/home/soundscape/.secrets/restic_password
export RESTIC_REPOSITORY='sftp://u497612@u497612.your-storagebox.de:23/restic'

# ntfy.sh topic for notifications 
NTFY_TOPIC="soundscape-community-backup"

# Base URL for log file access
LOG_BASE_URL="https://ntfy.soundscape.services"

# Log directory
LOG_DIR="/home/soundscape/.local/var/log/backup"
mkdir -p "${LOG_DIR}"

# Function to send ntfy notification
notify() {
  local priority="${1:-default}"
  local message="${2}"
  local click_url="${3:-}"
  
  if [ -n "${click_url}" ]; then
    curl -H "Priority: ${priority}" \
         -H "Click: ${click_url}" \
         -d "${message}" \
         "ntfy.sh/${NTFY_TOPIC}"
  else
    curl -H "Priority: ${priority}" -d "${message}" "ntfy.sh/${NTFY_TOPIC}"
  fi
}

# Function to generate random string for log filename
generate_random_suffix() {
  head -c 4 /dev/urandom | base32 | head -c 4
}

# Function to run restic with logging and error handling
run_restic_backup() {
  local job_name="${1}"
  shift
  local restic_args=("$@")
  
  local timestamp=$(date +%Y%m%d_%H%M)
  local random_suffix=$(generate_random_suffix)
  local log_filename="${job_name}_${timestamp}_${random_suffix}.txt"
  local temp_log=$(mktemp)
  
  # Run restic and capture output
  restic "${restic_args[@]}" > "${temp_log}" 2>&1
  local exit_code=$?
  
  if [ ${exit_code} -ne 0 ]; then
    # Move log to permanent location on failure
    mv "${temp_log}" "${LOG_DIR}/${log_filename}"
    local log_url="${LOG_BASE_URL}/${log_filename}"
    notify "high" "Backup failed: ${job_name}" "${log_url}"
    return 1
  fi
  # Clean up temp log on success
  rm -f "${temp_log}"
}

# Track if any backup fails
BACKUP_FAILED=0

# backup the activities and media files for the authoring tool
run_restic_backup "share-soundscape-services" backup /home/soundscape/share.soundscape.services
[ $? -ne 0 ] && BACKUP_FAILED=1

# backup the authoring tool database
run_restic_backup "authoring-tool-db" backup --stdin-filename=authoring-tool-production.sql \
  --stdin-from-command -- \
  docker compose -f /home/soundscape/authoring-tool2/docker-compose.yml exec postgres pg_dump -U postgres postgres
[ $? -ne 0 ] && BACKUP_FAILED=1

# Check if any backups failed
if [ ${BACKUP_FAILED} -eq 1 ]; then
  exit 1
fi

notify "default" "All backups completed successfully"

run_restic_backup "cleanup-old-backups" forget --keep-daily 30 \\keep-weekly 52 \
  --keep-yearly 3 --prune
