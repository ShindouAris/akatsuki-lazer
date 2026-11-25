package spectator

import (
	"context"
	"encoding/base64"
	"log/slog"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	replayKeyPrefix  = "hub:replay:frames:"
	maxReplayBundles = 20000
	replayTTL        = 6 * time.Hour
	sinkQueueSize    = 4096
)

// RedisFrames stores the frames of plays in the Redis lists the Python service reads at score submission.
// Each entry is base64 of the msgpack bundle (the Python reader also accepts its older JSON entries).
type RedisFrames struct {
	Client *redis.Client
}

// Open implements FrameStore.
func (r *RedisFrames) Open(scoreToken int64) FrameSink {
	s := &redisSink{
		client: r.Client,
		key:    replayKeyPrefix + strconv.FormatInt(scoreToken, 10),
		queue:  make(chan []byte, sinkQueueSize),
		done:   make(chan struct{}),
	}
	go s.run()
	return s
}

type redisSink struct {
	client *redis.Client
	key    string
	queue  chan []byte
	done   chan struct{}
	closed bool
}

// Append is only called from the owning connection's dispatch loop, so it needs no lock against Close, which
// the hub calls after taking the play away from the connection.
func (s *redisSink) Append(bundle []byte) {
	if s.closed {
		return
	}
	select {
	case s.queue <- bundle:
	default:
		slog.Warn("spectator: replay queue full, dropping a bundle", "key", s.key)
	}
}

func (s *redisSink) Close(ctx context.Context) error {
	if !s.closed {
		s.closed = true
		close(s.queue)
	}
	select {
	case <-s.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *redisSink) run() {
	defer close(s.done)
	for bundle := range s.queue {
		batch := [][]byte{bundle}
		// Write whatever else is already waiting in one round trip.
	drain:
		for len(batch) < 64 {
			select {
			case more, ok := <-s.queue:
				if !ok {
					break drain
				}
				batch = append(batch, more)
			default:
				break drain
			}
		}
		s.write(batch)
	}
}

func (s *redisSink) write(batch [][]byte) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	values := make([]any, len(batch))
	for i, b := range batch {
		values[i] = base64.StdEncoding.EncodeToString(b)
	}
	pipe := s.client.Pipeline()
	pipe.RPush(ctx, s.key, values...)
	pipe.LTrim(ctx, s.key, -maxReplayBundles, -1)
	pipe.Expire(ctx, s.key, replayTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		slog.Error("spectator: cannot store replay frames", "key", s.key, "error", err)
	}
}
