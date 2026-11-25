// Command spectator runs the spectator hub (live gameplay streaming).
//
// Configuration (environment):
//
//	LISTEN_ADDR           public hub address, TLS when the cert is set (default ":8082")
//	INTERNAL_LISTEN_ADDR  plain-HTTP address for the Python service's calls (default ":8083")
//	JWT_PUBLIC_KEY_FILE   PEM public key used to verify access tokens (default "../cert/public.pem")
//	JWT_AUDIENCE          expected token audience (default "5")
//	TLS_CERT_FILE / TLS_KEY_FILE  serve HTTPS when both are set
//	PYTHON_API_URL, INTERNAL_API_KEY, API_CA_FILE  the Python internal API (required)
//	REDIS_URL             Redis holding the replay buffers (default "redis://127.0.0.1:6379/0")
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

	"github.com/redis/go-redis/v9"

	"akatsuki/multiplayer/internal/auth"
	"akatsuki/multiplayer/internal/signalr"
	"akatsuki/multiplayer/internal/spectator"
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))

	verifier, err := auth.NewVerifierFromFile(env("JWT_PUBLIC_KEY_FILE", "../cert/public.pem"), env("JWT_AUDIENCE", "5"))
	if err != nil {
		slog.Error("cannot load the JWT public key", "error", err)
		os.Exit(1)
	}

	apiURL, key := os.Getenv("PYTHON_API_URL"), os.Getenv("INTERNAL_API_KEY")
	if apiURL == "" || key == "" {
		slog.Error("PYTHON_API_URL and INTERNAL_API_KEY are required")
		os.Exit(1)
	}
	httpClient, err := apiClient(os.Getenv("API_CA_FILE"))
	if err != nil {
		slog.Error("cannot load API_CA_FILE", "error", err)
		os.Exit(1)
	}

	redisOptions, err := redis.ParseURL(env("REDIS_URL", "redis://127.0.0.1:6379/0"))
	if err != nil {
		slog.Error("bad REDIS_URL", "error", err)
		os.Exit(1)
	}
	rdb := redis.NewClient(redisOptions)
	defer rdb.Close()

	hub := spectator.New(verifier, spectator.Options{
		Backend: &spectator.PythonAPI{BaseURL: apiURL, APIKey: key, Client: httpClient},
		Frames:  &spectator.RedisFrames{Client: rdb},
	})

	server := &signalr.Server{Handler: hub}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /spectator/negotiate", server.Negotiate)
	mux.HandleFunc("GET /spectator", server.ServeWS)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"healthy"}`))
	})

	httpServer := &http.Server{Addr: env("LISTEN_ADDR", ":8082"), Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	internalServer := &http.Server{Addr: env("INTERNAL_LISTEN_ADDR", ":8083"), Handler: hub.InternalHandler(key), ReadHeaderTimeout: 10 * time.Second}

	go func() {
		if err := internalServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("internal server stopped", "error", err)
			os.Exit(1)
		}
	}()

	shutdown := make(chan struct{})
	go func() {
		defer close(shutdown)
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(ctx)
		_ = internalServer.Shutdown(ctx)
	}()

	cert, tlsKey := os.Getenv("TLS_CERT_FILE"), os.Getenv("TLS_KEY_FILE")
	slog.Info("spectator hub listening", "addr", httpServer.Addr, "internal", internalServer.Addr, "tls", cert != "" && tlsKey != "")
	if cert != "" && tlsKey != "" {
		err = httpServer.ListenAndServeTLS(cert, tlsKey)
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
