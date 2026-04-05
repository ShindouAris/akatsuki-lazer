#!/bin/bash
if [ ! -f .init_db.done ]; then
    echo "Initializing database..."
    uv run init_db.py && touch .init_db.done
fi
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000