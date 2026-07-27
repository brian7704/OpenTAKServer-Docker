#!/usr/bin/env bash
# Create a marker on the OpenTAKServer map via POST /api/markers.
# The marker is broadcast to connected TAK clients and stored in the DB.
set -euo pipefail

OTS_HOST="${OTS_HOST:-192.168.1.29}"
OTS_USER="${OTS_USER:-administrator}"
OTS_PASS="${OTS_PASS:-password}"

usage() {
    cat <<EOF
Usage: $(basename "$0") -n NAME -a LAT -o LON [-t TYPE] [-u UUID]

Required:
  -n NAME   Callsign shown on the map
  -a LAT    Latitude  (-90..90)
  -o LON    Longitude (-180..180)

Optional:
  -t TYPE   CoT type (default: a-u-G)
              a-f-G-U-C  friendly ground infantry (blue)
              a-h-G      hostile ground (red)
              a-n-G      neutral ground (green)
              a-u-G      unknown ground (yellow)
  -u UUID   Marker UID, must be UUID4. Generated if omitted.
            Reusing an existing UID updates that marker.
  -l        List current markers and exit
  -d UUID   Delete the marker with this UID and exit

Environment:
  OTS_HOST  server address (default: $OTS_HOST)
  OTS_USER  username       (default: $OTS_USER)
  OTS_PASS  password       (default: password)

Examples:
  $(basename "$0") -n "OP North" -a 52.2297 -o 21.0122 -t a-f-G-U-C
  OTS_HOST=10.0.0.5 $(basename "$0") -n Rally -a 52.1 -o 21.0
EOF
    exit "${1:-1}"
}

name="" lat="" lon="" cot_type="a-u-G" muid="" action="add" del_uid=""

while getopts ":n:a:o:t:u:d:lh" opt; do
    case "$opt" in
        n) name="$OPTARG" ;;
        a) lat="$OPTARG" ;;
        o) lon="$OPTARG" ;;
        t) cot_type="$OPTARG" ;;
        u) muid="$OPTARG" ;;
        l) action="list" ;;
        d) action="delete"; del_uid="$OPTARG" ;;
        h) usage 0 ;;
        :) echo "Error: -$OPTARG requires an argument" >&2; usage ;;
        \?) echo "Error: unknown option -$OPTARG" >&2; usage ;;
    esac
done

# Log in and grab a bearer token. Flask-Security returns it under
# response.user.authentication_token when include_auth_token is set.
login() {
    local body
    body=$(curl -sk --fail-with-body \
        -X POST "https://${OTS_HOST}/api/login?include_auth_token" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${OTS_USER}\",\"password\":\"${OTS_PASS}\"}") || {
        echo "Error: login failed for ${OTS_USER}@${OTS_HOST}" >&2
        exit 1
    }
    python3 -c "
import json, sys
try:
    print(json.load(sys.stdin)['response']['user']['authentication_token'])
except (KeyError, ValueError):
    sys.exit('Error: no auth token in login response')
" <<<"$body"
}

token=$(login)

if [[ "$action" == "list" ]]; then
    curl -sk "https://${OTS_HOST}/api/markers" \
        -H "Authentication-Token: ${token}" | python3 -m json.tool
    exit 0
fi

if [[ "$action" == "delete" ]]; then
    curl -sk -X DELETE "https://${OTS_HOST}/api/markers?uid=${del_uid}" \
        -H "Authentication-Token: ${token}"
    echo
    exit 0
fi

[[ -n "$name" && -n "$lat" && -n "$lon" ]] || {
    echo "Error: -n, -a and -o are required" >&2
    usage
}

# The API rejects anything that is not UUID4, so generate rather than improvise.
if [[ -z "$muid" ]]; then
    muid=$(python3 -c "import uuid; print(uuid.uuid4())")
fi

payload=$(python3 -c "
import json, sys
print(json.dumps({
    'uid': sys.argv[1],
    'name': sys.argv[2],
    'latitude': float(sys.argv[3]),
    'longitude': float(sys.argv[4]),
    'type': sys.argv[5],
}))
" "$muid" "$name" "$lat" "$lon" "$cot_type")

response=$(curl -sk -X POST "https://${OTS_HOST}/api/markers" \
    -H "Content-Type: application/json" \
    -H "Authentication-Token: ${token}" \
    -d "$payload")

if [[ "$response" == *'"success":true'* ]]; then
    echo "Created '${name}' (${cot_type}) at ${lat},${lon}"
    echo "  uid: ${muid}"
else
    echo "Failed: ${response}" >&2
    exit 1
fi
