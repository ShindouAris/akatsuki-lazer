package metadata

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// PythonAPI is the Backend served by the Python service's internal API.
type PythonAPI struct {
	BaseURL string
	APIKey  string
	Client  *http.Client
}

func (p *PythonAPI) get(ctx context.Context, path string, out any) error {
	client := p.Client
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(p.BaseURL, "/")+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-Internal-Key", p.APIKey)
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("GET %s: status %d", path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// Friends implements Backend.
func (p *PythonAPI) Friends(ctx context.Context, userID int32) ([]int32, error) {
	var out struct {
		FriendIDs []int32 `json:"friend_ids"`
	}
	err := p.get(ctx, "/internal/metadata/friends/"+strconv.Itoa(int(userID)), &out)
	return out.FriendIDs, err
}

// RoomStats implements Backend.
func (p *PythonAPI) RoomStats(ctx context.Context, roomID int32) ([]any, error) {
	var out struct {
		Items []struct {
			PlaylistItemID       int64   `json:"playlist_item_id"`
			TotalScoreDistribute []int64 `json:"total_score_distribution"`
			CumulativeScore      int64   `json:"cumulative_score"`
			LastProcessedScoreID int64   `json:"last_processed_score_id"`
		} `json:"items"`
	}
	if err := p.get(ctx, "/internal/metadata/rooms/"+strconv.Itoa(int(roomID))+"/stats", &out); err != nil {
		return nil, err
	}
	stats := make([]any, 0, len(out.Items))
	for _, item := range out.Items {
		stats = append(stats, []any{item.PlaylistItemID, item.TotalScoreDistribute, item.CumulativeScore, item.LastProcessedScoreID})
	}
	return stats, nil
}

// DailyChallenge implements Backend.
func (p *PythonAPI) DailyChallenge(ctx context.Context) (int32, bool, error) {
	var out struct {
		RoomID *int32 `json:"room_id"`
	}
	if err := p.get(ctx, "/internal/metadata/daily-challenge", &out); err != nil {
		return 0, false, err
	}
	if out.RoomID == nil {
		return 0, false, nil
	}
	return *out.RoomID, true, nil
}

// RedisUpdates reads the beatmap update queue the Python service writes (see HubStateService).
type RedisUpdates struct {
	Client *redis.Client
	Limit  int64
}

const (
	updateIDsKey   = "hub:metadata:beatmap_updates:ids"
	updateEntryKey = "hub:metadata:beatmap_updates:entry:"
)

// Since implements Updates.
func (r *RedisUpdates) Since(ctx context.Context, queueID int64) ([]int32, int64, error) {
	if queueID < 0 {
		queueID = 0
	}
	limit := r.Limit
	if limit <= 0 {
		limit = 500
	}
	rows, err := r.Client.ZRangeByScore(ctx, updateIDsKey, &redis.ZRangeBy{
		Min: "(" + strconv.FormatInt(queueID, 10), Max: "+inf", Offset: 0, Count: limit,
	}).Result()
	if err != nil {
		return nil, queueID, err
	}
	if len(rows) == 0 {
		return nil, queueID, nil
	}
	keys := make([]string, len(rows))
	for i, row := range rows {
		keys[i] = updateEntryKey + row
	}
	payloads, err := r.Client.MGet(ctx, keys...).Result()
	if err != nil {
		return nil, queueID, err
	}

	latest := queueID
	seen := map[int32]struct{}{}
	var ids []int32
	for i, row := range rows {
		if id, err := strconv.ParseInt(row, 10, 64); err == nil && id > latest {
			latest = id
		}
		text, ok := payloads[i].(string)
		if !ok {
			continue
		}
		var set []int32
		if json.Unmarshal([]byte(text), &set) != nil {
			continue
		}
		for _, id := range set {
			if _, dup := seen[id]; id > 0 && !dup {
				seen[id] = struct{}{}
				ids = append(ids, id)
			}
		}
	}
	return ids, latest, nil
}

// InternalHandler serves the endpoints the Python service calls, guarded by the shared internal key.
func (h *Hub) InternalHandler(apiKey string) http.Handler {
	guard := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Internal-Key")), []byte(apiKey)) != 1 {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			next(w, r)
		}
	}
	reply := func(w http.ResponseWriter, body any) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(body)
	}
	decode := func(w http.ResponseWriter, r *http.Request, into any) bool {
		var buf bytes.Buffer
		if _, err := buf.ReadFrom(http.MaxBytesReader(w, r.Body, 1<<20)); err != nil || json.Unmarshal(buf.Bytes(), into) != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return false
		}
		return true
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /internal/online-count", guard(func(w http.ResponseWriter, _ *http.Request) {
		reply(w, map[string]int{"online_users": h.OnlineUsers()})
	}))
	mux.HandleFunc("POST /internal/room-score-set", guard(func(w http.ResponseWriter, r *http.Request) {
		var e struct {
			RoomID         int32  `json:"room_id"`
			PlaylistItemID int64  `json:"playlist_item_id"`
			ScoreID        int64  `json:"score_id"`
			UserID         int32  `json:"user_id"`
			TotalScore     int64  `json:"total_score"`
			NewRank        *int64 `json:"new_rank"`
		}
		if !decode(w, r, &e) {
			return
		}
		sent := h.NotifyRoomScoreSet(e.RoomID, []any{e.RoomID, e.PlaylistItemID, e.ScoreID, e.UserID, e.TotalScore, e.NewRank})
		reply(w, map[string]int{"sent": sent})
	}))
	mux.HandleFunc("POST /internal/beatmap-updates", guard(func(w http.ResponseWriter, r *http.Request) {
		var u struct {
			BeatmapSetIDs []int32 `json:"beatmap_set_ids"`
			QueueID       int64   `json:"queue_id"`
		}
		if !decode(w, r, &u) {
			return
		}
		reply(w, map[string]int{"notified": h.NotifyBeatmapUpdates(u.BeatmapSetIDs, u.QueueID)})
	}))
	return mux
}
