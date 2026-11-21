package main

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// Pairing handoff files.
//
// The REST server only starts after pairing succeeds, so while a QR code is
// on screen nothing is listening on the bridge's port. A headless deployment
// (no terminal to read the QR from) therefore needs another way to get at the
// code: the bridge writes each code it emits to pairQRFile, and a companion
// web page re-encodes it as a scannable QR.
//
// pairQRFile holds two lines: the Unix time the code was issued, then the
// code. whatsmeow rotates codes about every 20 seconds, so readers should
// treat an old timestamp as stale.
//
// loggedOutFile is an empty marker that exists while WhatsApp has rejected
// the session. A rejected session still leaves a device row in whatsapp.db,
// so without the marker a reader could not tell "logged out" from "linked".
const (
	pairQRFile    = "store/pair_qr.txt"
	loggedOutFile = "store/pair_logged_out"
)

// writePairQR publishes a pairing code, replacing any previous one. The write
// is atomic so a reader never sees a half-written file.
func writePairQR(path, code string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	content := fmt.Sprintf("%d\n%s\n", time.Now().Unix(), code)
	if err := os.WriteFile(tmp, []byte(content), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// clearPairQR removes the published code once it can no longer be scanned.
func clearPairQR(path string) {
	_ = os.Remove(path)
}

// markLoggedOut records that WhatsApp has logged this device out.
func markLoggedOut(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, nil, 0o600)
}

// clearLoggedOut removes the logged-out marker after a successful connection.
func clearLoggedOut(path string) {
	_ = os.Remove(path)
}

// retryDelay is the wait before connection attempt number attempt+1: 5s,
// doubling each time, capped at one minute.
func retryDelay(attempt int) time.Duration {
	const (
		base    = 5 * time.Second
		ceiling = time.Minute
	)
	if attempt < 1 {
		attempt = 1
	}
	delay := base
	for i := 1; i < attempt && delay < ceiling; i++ {
		delay *= 2
	}
	if delay > ceiling {
		delay = ceiling
	}
	return delay
}
