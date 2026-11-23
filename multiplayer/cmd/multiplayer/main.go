// Command multiplayer runs the realtime multiplayer hub.
//
// Configuration (environment):
//
//	LISTEN_ADDR          address to listen on (default ":8081")
//	JWT_PUBLIC_KEY_FILE  PEM public key used to verify access tokens (default "../cert/public.pem")
//	JWT_AUDIENCE         expected token audience, the OAuth client id (default "5")
//	TLS_CERT_FILE / TLS_KEY_FILE  serve HTTPS when both are set (the osu! client needs HTTPS)
//
// Persistence (see README): with PYTHON_API_URL set, rooms and playlist items are stored in the Python
// service's database through its internal API, which also makes score submission in rooms work.
//
//	PYTHON_API_URL       base URL of the Python API, e.g. http://127.0.0.1:8000
//	INTERNAL_API_KEY     must equal the Python side's INTERNAL_API_KEY
//	API_CA_FILE          optional PEM authority to trust for an https PYTHON_API_URL (development CA)
//
// The same setting turns on matchmaking: ranked play, duels and the queue (pools are rows of the Python database,
// seeded with `python -m app.seeders matchmaking`).
//
// Without PYTHON_API_URL rooms live in memory only and are lost on restart (development only).
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"akatsuki/multiplayer/internal/auth"
	"akatsuki/multiplayer/internal/hub"
	"akatsuki/multiplayer/internal/matchmaking"
	"akatsuki/multiplayer/internal/room"
	"akatsuki/multiplayer/internal/signalr"
	"akatsuki/multiplayer/internal/store"
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))

	verifier, err := auth.NewVerifierFromFile(env("JWT_PUBLIC_KEY_FILE", "../cert/public.pem"), env("JWT_AUDIENCE", "5"))
	if err != nil {
		slog.Error("cannot load the JWT public key", "error", err)
		os.Exit(1)
	}

	options := room.Options{}
	var persistence *store.Client
	if apiURL := os.Getenv("PYTHON_API_URL"); apiURL != "" {
		key := os.Getenv("INTERNAL_API_KEY")
		if key == "" {
			slog.Error("PYTHON_API_URL is set but INTERNAL_API_KEY is empty")
			os.Exit(1)
		}
		httpClient, err := apiClient(os.Getenv("API_CA_FILE"))
		if err != nil {
			slog.Error("cannot load API_CA_FILE", "error", err)
			os.Exit(1)
		}
		persistence = store.New(store.Config{BaseURL: apiURL, APIKey: key, HTTPClient: httpClient})

		// Rooms left open by a previous run have no live state any more. A failure here means the API is
		// unreachable or the key is wrong, so refuse to start rather than run without persistence.
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		closed, err := persistence.CloseStaleRooms(ctx)
		cancel()
		if err != nil {
			slog.Error("cannot reach the Python internal API", "url", apiURL, "error", err)
			os.Exit(1)
		}
		slog.Info("closed rooms left over from a previous run", "count", closed)

		persistence.Start()
		options.Store = persistence
		options.Beatmaps = persistence
	} else {
		slog.Warn("PYTHON_API_URL is not set: rooms are kept in memory only and score submission in rooms will not link up")
	}

	multiplayer := hub.New(verifier, options)
	if persistence != nil {
		multiplayer.SetRelations(persistence)
	}

	// The matchmaking queue (ranked play and duels) reads pools and ratings from, and records results in, the
	// Python service, so it exists only together with persistence.
	var queue *matchmaking.Service
	queueCtx, stopQueue := context.WithCancel(context.Background())
	defer stopQueue()
	if persistence != nil {
		queue = matchmaking.New(matchmaking.Config{
			Directory: persistence,
			Backend:   persistence,
			Rooms:     multiplayer.Rooms(),
			Notifier:  multiplayer,
		})
		multiplayer.SetMatchmaking(queue)
		go queue.Run(queueCtx)
	} else {
		slog.Warn("matchmaking is disabled without PYTHON_API_URL")
	}

	server := &signalr.Server{Handler: multiplayer}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /multiplayer/negotiate", server.Negotiate)
	mux.HandleFunc("GET /multiplayer", server.ServeWS)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"healthy"}`))
	})

	httpServer := &http.Server{Addr: env("LISTEN_ADDR", ":8081"), Handler: mux, ReadHeaderTimeout: 10 * time.Second}

	shutdown := make(chan struct{})
	go func() {
		defer close(shutdown)
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
		<-stop

		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(ctx)

		// Stop pairing players, and let the matches being created finish before their rooms are closed.
		stopQueue()
		if queue != nil {
			queue.Wait()
		}

		// Mark every room closed and deliver the last updates before exiting.
		multiplayer.Rooms().CloseAll()
		if persistence != nil {
			if err := persistence.Stop(ctx); err != nil {
				slog.Error("some room updates could not be saved", "error", err)
			}
		}
	}()

	cert, key := os.Getenv("TLS_CERT_FILE"), os.Getenv("TLS_KEY_FILE")
	slog.Info("multiplayer hub listening", "addr", httpServer.Addr, "tls", cert != "" && key != "", "persistent", persistence != nil)
	if cert != "" && key != "" {
		err = httpServer.ListenAndServeTLS(cert, key)
	} else {
		err = httpServer.ListenAndServe()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server stopped", "error", err)
		os.Exit(1)
	}
	<-shutdown
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
