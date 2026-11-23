# Cross-language end-to-end check

Drives the Go hub (real WebSocket, real HTTP store) against the **real Python internal router** on a throwaway
SQLite database, then inspects the database.

```bash
# from the repository root, with the project's Python environment
export E2E_DB=/tmp/e2e.db INTERNAL_API_KEY=e2e-key
python multiplayer/e2e/api_server.py &                       # serves http://127.0.0.1:18000

cd multiplayer
E2E_API_URL=http://127.0.0.1:18000 E2E_API_KEY=e2e-key \
  go test -count=1 -v -run TestAgainstTheRealPythonAPI ./internal/hub/    # prints E2E_ROOM_ID / E2E_FIRST_ITEM

cd ..
python multiplayer/e2e/verify.py <E2E_ROOM_ID> <E2E_FIRST_ITEM>
```
