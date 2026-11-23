// Package auth verifies the access tokens issued by the Python API (RS256, `sub` = user id).
package auth

import (
	"crypto/rsa"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/golang-jwt/jwt/v5"
)

// Verifier validates bearer tokens.
type Verifier struct {
	key      *rsa.PublicKey
	audience string
}

// NewVerifier builds a verifier from a PEM encoded RSA public key.
func NewVerifier(publicKeyPEM []byte, audience string) (*Verifier, error) {
	key, err := jwt.ParseRSAPublicKeyFromPEM(publicKeyPEM)
	if err != nil {
		return nil, fmt.Errorf("parsing public key: %w", err)
	}
	return &Verifier{key: key, audience: audience}, nil
}

// NewVerifierFromFile reads the public key from disk.
func NewVerifierFromFile(path, audience string) (*Verifier, error) {
	pem, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return NewVerifier(pem, audience)
}

// UserID validates an access token and returns the user it was issued to.
func (v *Verifier) UserID(token string) (int32, error) {
	claims := jwt.MapClaims{}
	options := []jwt.ParserOption{jwt.WithValidMethods([]string{"RS256"}), jwt.WithExpirationRequired()}
	if v.audience != "" {
		options = append(options, jwt.WithAudience(v.audience))
	}

	if _, err := jwt.ParseWithClaims(token, claims, func(*jwt.Token) (any, error) { return v.key, nil }, options...); err != nil {
		return 0, err
	}
	if kind, _ := claims["type"].(string); kind != "" && kind != "access" {
		return 0, errors.New("not an access token")
	}

	subject, err := claims.GetSubject()
	if err != nil || subject == "" {
		return 0, errors.New("token has no subject")
	}
	id, err := strconv.ParseInt(subject, 10, 32)
	if err != nil || id <= 0 {
		return 0, errors.New("token subject is not a user id")
	}
	return int32(id), nil
}

// Authenticate implements the hub's request authentication: `Authorization: Bearer` or `?access_token=`.
func (v *Verifier) Authenticate(r *http.Request) (int32, bool) {
	token := ""
	if header := r.Header.Get("Authorization"); len(header) > 7 && strings.EqualFold(header[:7], "bearer ") {
		token = strings.TrimSpace(header[7:])
	}
	if token == "" {
		token = r.URL.Query().Get("access_token")
	}
	if token == "" {
		return 0, false
	}
	id, err := v.UserID(token)
	return id, err == nil
}
