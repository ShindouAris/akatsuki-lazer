#!/bin/bash
# The image's virtualenv is first on PATH (see the Dockerfile).
if [ ! -f .init_db.done ]; then
    echo "Initializing database..."
    python init_db.py && touch .init_db.done
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
