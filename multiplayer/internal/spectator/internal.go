package spectator

import (
	"crypto/subtle"
	"encoding/json"
	"net/http"
)

// InternalHandler serves the endpoints the Python service calls, guarded by the shared internal key.
func (h *Hub) InternalHandler(apiKey string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /internal/score-processed", func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Internal-Key")), []byte(apiKey)) != 1 {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var body struct {
			UserID  int32 `json:"user_id"`
			ScoreID int64 `json:"score_id"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&body); err != nil || body.UserID == 0 {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		h.NotifyScoreProcessed(body.UserID, body.ScoreID)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	return mux
}
