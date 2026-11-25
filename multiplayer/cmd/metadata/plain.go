package main

import (
	"os"
	"strings"
)

// SSL=false (the same switch the Python app reads from .env) turns TLS off: the hub serves plain HTTP and
// talks to the Python API over http, for deployments where nginx or a Cloudflare Tunnel terminates TLS.
func init() {
	switch strings.ToLower(os.Getenv("SSL")) {
	case "0", "false", "no", "off":
		os.Unsetenv("TLS_CERT_FILE")
		os.Unsetenv("TLS_KEY_FILE")
		os.Unsetenv("API_CA_FILE")
		if u := os.Getenv("PYTHON_API_URL"); strings.HasPrefix(u, "https://") {
			os.Setenv("PYTHON_API_URL", "http://"+strings.TrimPrefix(u, "https://"))
		}
	}
}
