package main

import (
	"crypto/tls"
	"crypto/x509"
	"os"
	"path/filepath"
	"testing"
)

func TestTheServerCertificateIsTrustedThroughTheAuthority(t *testing.T) {
	dir := t.TempDir()
	if err := run(dir, []string{"10.1.2.3", "dev.example"}, false, false); err != nil {
		t.Fatal(err)
	}

	pair, err := tls.LoadX509KeyPair(filepath.Join(dir, "localhost.pem"), filepath.Join(dir, "localhost-key.pem"))
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}

	caPEM, err := os.ReadFile(filepath.Join(dir, "ca.pem"))
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		t.Fatal("the authority file holds no certificate")
	}

	for _, name := range []string{"localhost", "127.0.0.1", "::1", "10.1.2.3", "dev.example"} {
		if _, err := leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: name}); err != nil {
			t.Errorf("not valid for %s: %v", name, err)
		}
	}
	if _, err := leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: "elsewhere.example"}); err == nil {
		t.Error("valid for a name it was not made for")
	}
	if _, err := leaf.Verify(x509.VerifyOptions{Roots: x509.NewCertPool(), DNSName: "localhost"}); err == nil {
		t.Error("valid without trusting the authority")
	}
}

func TestExistingCertificatesAreKeptUnlessForced(t *testing.T) {
	dir := t.TempDir()
	if err := run(dir, nil, false, false); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(filepath.Join(dir, "localhost.pem"))

	if err := run(dir, nil, false, false); err != nil {
		t.Fatal(err)
	}
	if after, _ := os.ReadFile(filepath.Join(dir, "localhost.pem")); string(after) != string(before) {
		t.Error("a second run replaced the certificate")
	}

	if err := run(dir, nil, true, false); err != nil {
		t.Fatal(err)
	}
	if after, _ := os.ReadFile(filepath.Join(dir, "localhost.pem")); string(after) == string(before) {
		t.Error("-force did not replace the certificate")
	}
}

func TestKeepCAIssuesANewServerCertificateFromTheSameAuthority(t *testing.T) {
	dir := t.TempDir()
	if err := run(dir, nil, false, false); err != nil {
		t.Fatal(err)
	}
	caBefore, err := os.ReadFile(filepath.Join(dir, "ca.pem"))
	if err != nil {
		t.Fatal(err)
	}

	if err := run(dir, []string{"app"}, false, true); err != nil {
		t.Fatal(err)
	}
	caAfter, err := os.ReadFile(filepath.Join(dir, "ca.pem"))
	if err != nil {
		t.Fatal(err)
	}
	if string(caBefore) != string(caAfter) {
		t.Fatal("the authority was replaced, so it would have to be trusted again")
	}

	pair, err := tls.LoadX509KeyPair(filepath.Join(dir, "localhost.pem"), filepath.Join(dir, "localhost-key.pem"))
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM(caBefore)
	for _, name := range []string{"app", "localhost"} {
		if _, err := leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: name}); err != nil {
			t.Errorf("not valid for %s: %v", name, err)
		}
	}
}
