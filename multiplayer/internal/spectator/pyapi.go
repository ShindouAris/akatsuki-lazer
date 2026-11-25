package spectator

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// PythonAPI is the Backend served by the Python service's internal API.
type PythonAPI struct {
	BaseURL string
	APIKey  string
	Client  *http.Client
}

func (p *PythonAPI) client() *http.Client {
	if p.Client != nil {
		return p.Client
	}
	return &http.Client{Timeout: 10 * time.Second}
}

func (p *PythonAPI) do(ctx context.Context, method, path string, body any, out any) error {
	var reader *bytes.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	} else {
		reader = bytes.NewReader(nil)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(p.BaseURL, "/")+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("X-Internal-Key", p.APIKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.client().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s %s: status %d", method, path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// ValidatePlay implements Backend.
func (p *PythonAPI) ValidatePlay(ctx context.Context, userID int32, scoreToken int64, beatmapID, rulesetID *int32) (bool, string, error) {
	var out struct {
		Valid  bool   `json:"valid"`
		Reason string `json:"reason"`
	}
	err := p.do(ctx, http.MethodPost, "/internal/spectator/validate-play", map[string]any{
		"user_id":     userID,
		"score_token": scoreToken,
		"beatmap_id":  beatmapID,
		"ruleset_id":  rulesetID,
	}, &out)
	return out.Valid, out.Reason, err
}

// Username implements Backend.
func (p *PythonAPI) Username(ctx context.Context, userID int32) (string, error) {
	var out struct {
		Username string `json:"username"`
	}
	err := p.do(ctx, http.MethodGet, "/internal/spectator/users/"+url.PathEscape(fmt.Sprint(userID)), nil, &out)
	return out.Username, err
}
