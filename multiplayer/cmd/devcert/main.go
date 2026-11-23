// Command devcert creates a development certificate authority and a server certificate it signs, so that the osu!
// client (which insists on HTTPS) can talk to the Python API and the multiplayer service on this machine.
//
//	go run ./cmd/devcert                       # writes ../dev-certs/*
//	go run ./cmd/devcert -host 192.168.1.20    # also valid for another name or address
//
// Files written to the output directory:
//
//	ca.pem, ca-key.pem          the authority; trust ca.pem (and keep ca-key.pem to yourself)
//	localhost.pem               the server certificate followed by the authority
//	localhost-key.pem           its private key
//
// Existing files are kept unless -force is given, so running it again is harmless.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"flag"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type hosts []string

func (h *hosts) String() string     { return strings.Join(*h, ",") }
func (h *hosts) Set(v string) error { *h = append(*h, v); return nil }

func main() {
	var extra hosts
	out := flag.String("out", "../dev-certs", "output directory")
	force := flag.Bool("force", false, "replace existing files (a new authority too: it must be trusted again)")
	keepCA := flag.Bool("keep-ca", false, "issue a new server certificate from the existing authority")
	flag.Var(&extra, "host", "additional DNS name or IP address (repeatable)")
	flag.Parse()

	if err := run(*out, extra, *force, *keepCA); err != nil {
		fmt.Fprintln(os.Stderr, "devcert:", err)
		os.Exit(1)
	}
}

func run(dir string, extra []string, force, keepCA bool) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	paths := map[string]string{
		"ca": filepath.Join(dir, "ca.pem"), "cakey": filepath.Join(dir, "ca-key.pem"),
		"cert": filepath.Join(dir, "localhost.pem"), "key": filepath.Join(dir, "localhost-key.pem"),
	}
	if !force && !keepCA {
		exists := true
		for _, p := range paths {
			if _, err := os.Stat(p); err != nil {
				exists = false
			}
		}
		if exists {
			fmt.Println("certificates already exist in", dir, "(use -force to replace them)")
			return nil
		}
	}

	now := time.Now().Add(-time.Hour)

	var (
		caKey  *ecdsa.PrivateKey
		caDER  []byte
		caCert *x509.Certificate
		err    error
	)
	if keepCA {
		caKey, caDER, caCert, err = loadCA(paths["ca"], paths["cakey"])
		if err != nil {
			return err
		}
	} else {
		caKey, err = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return err
		}
		caTemplate := &x509.Certificate{
			SerialNumber:          serial(),
			Subject:               pkix.Name{CommonName: "akatsuki-lazer development CA", Organization: []string{"akatsuki-lazer dev"}},
			NotBefore:             now,
			NotAfter:              now.AddDate(5, 0, 0),
			IsCA:                  true,
			BasicConstraintsValid: true,
			KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
			MaxPathLenZero:        true,
		}
		caDER, err = x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
		if err != nil {
			return err
		}
		caCert, err = x509.ParseCertificate(caDER)
		if err != nil {
			return err
		}
	}

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	template := &x509.Certificate{
		SerialNumber: serial(),
		Subject:      pkix.Name{CommonName: "localhost", Organization: []string{"akatsuki-lazer dev"}},
		NotBefore:    now,
		NotAfter:     now.AddDate(1, 0, 0),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     []string{"localhost"},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")},
	}
	for _, h := range extra {
		if ip := net.ParseIP(h); ip != nil {
			template.IPAddresses = append(template.IPAddresses, ip)
		} else {
			template.DNSNames = append(template.DNSNames, h)
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, caCert, &key.PublicKey, caKey)
	if err != nil {
		return err
	}

	caKeyDER, err := x509.MarshalECPrivateKey(caKey)
	if err != nil {
		return err
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return err
	}

	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	leafPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	files := []struct {
		path string
		data []byte
		mode os.FileMode
	}{
		{paths["ca"], caPEM, 0o644},
		{paths["cakey"], pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: caKeyDER}), 0o600},
		{paths["cert"], append(append([]byte{}, leafPEM...), caPEM...), 0o644},
		{paths["key"], pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}), 0o600},
	}
	if keepCA {
		files = files[2:] // the authority stays as it is
	}
	for _, f := range files {
		if err := os.WriteFile(f.path, f.data, f.mode); err != nil {
			return err
		}
	}

	fmt.Println("wrote", paths["ca"], paths["cert"], paths["key"])
	fmt.Println("valid for:", strings.Join(append([]string{"localhost", "127.0.0.1", "::1"}, extra...), ", "))
	fmt.Println("trust the authority once, for your user (Windows will ask you to confirm):")
	fmt.Printf("  certutil -user -addstore Root %q\n", paths["ca"])
	return nil
}

func loadCA(certPath, keyPath string) (*ecdsa.PrivateKey, []byte, *x509.Certificate, error) {
	certPEM, err := os.ReadFile(certPath)
	if err != nil {
		return nil, nil, nil, err
	}
	keyPEM, err := os.ReadFile(keyPath)
	if err != nil {
		return nil, nil, nil, err
	}
	certBlock, _ := pem.Decode(certPEM)
	keyBlock, _ := pem.Decode(keyPEM)
	if certBlock == nil || keyBlock == nil {
		return nil, nil, nil, fmt.Errorf("%s or %s is not PEM", certPath, keyPath)
	}
	cert, err := x509.ParseCertificate(certBlock.Bytes)
	tkey, err2 := x509.ParseECPrivateKey(keyBlock.Bytes)
	if err != nil {
		return nil, nil, nil, err
	}
	if err2 != nil {
		return nil, nil, nil, err2
	}
	return tkey, certBlock.Bytes, cert, nil
}

func serial() *big.Int {
	n, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	if err != nil {
		panic(err)
	}
	return n
}
