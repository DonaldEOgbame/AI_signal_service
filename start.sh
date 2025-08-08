#!/bin/bash
set -e
pip install -r requirements.txt
python AI_service/manage.py migrate --noinput
python AI_service/manage.py collectstatic --noinput
if [ -n "$GVISION_JSON_B64" ]; then
  echo "$GVISION_JSON_B64" | base64 -d > gvision.json
  export GOOGLE_APPLICATION_CREDENTIALS=$(pwd)/gvision.json
fi
python AI_service/manage.py runbot &
gunicorn AI_service.wsgi:application --bind 0.0.0.0:${PORT:-8000}
