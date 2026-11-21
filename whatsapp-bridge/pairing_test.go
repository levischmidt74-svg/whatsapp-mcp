package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestWritePairQR_WritesTimestampThenCodeAndReplaces(t *testing.T) {
	path := filepath.Join(t.TempDir(), "store", "pair_qr.txt")

	for _, code := range []string{"first-code", "second-code"} {
		before := time.Now().Unix()
		if err := writePairQR(path, code); err != nil {
			t.Fatalf("writePairQR(%q) failed: %v", code, err)
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("failed to read the code file: %v", err)
		}
		lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
		if len(lines) != 2 {
			t.Fatalf("expected 2 lines, got %q", raw)
		}
		issued, err := strconv.ParseInt(lines[0], 10, 64)
		if err != nil || issued < before || issued > time.Now().Unix() {
			t.Errorf("first line should be the issue time, got %q", lines[0])
		}
		if lines[1] != code {
			t.Errorf("second line = %q, want %q", lines[1], code)
		}
	}

	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Errorf("temporary file was left behind")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("code file mode = %o, want 600", perm)
	}

	clearPairQR(path)
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("clearPairQR should remove the file")
	}
	clearPairQR(path) // removing a missing file must not fail
}

func TestLoggedOutMarker(t *testing.T) {
	path := filepath.Join(t.TempDir(), "store", "pair_logged_out")

	if err := markLoggedOut(path); err != nil {
		t.Fatalf("markLoggedOut failed: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("marker should exist: %v", err)
	}
	if err := markLoggedOut(path); err != nil {
		t.Fatalf("marking twice should be fine: %v", err)
	}

	clearLoggedOut(path)
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("clearLoggedOut should remove the marker")
	}
	clearLoggedOut(path)
}

func TestRetryDelay(t *testing.T) {
	want := map[int]time.Duration{
		0:   5 * time.Second,
		1:   5 * time.Second,
		2:   10 * time.Second,
		3:   20 * time.Second,
		4:   40 * time.Second,
		5:   time.Minute,
		6:   time.Minute,
		500: time.Minute,
	}
	for attempt, expected := range want {
		if got := retryDelay(attempt); got != expected {
			t.Errorf("retryDelay(%d) = %v, want %v", attempt, got, expected)
		}
	}
}
