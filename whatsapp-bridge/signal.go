package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// Message signals tell an external service (Lawnsmith HQ) that a WhatsApp
// message arrived, so it can react on demand instead of polling. A signal
// carries routing metadata only: never the message text, media, captions or
// sender names.
//
// Signals are off unless both WHATSAPP_SIGNAL_URL and WHATSAPP_SIGNAL_SECRET
// are set. They are independent of the legacy WEBHOOK_URL webhook, which
// forwards message content.

const (
	signalURLEnv    = "WHATSAPP_SIGNAL_URL"
	signalSecretEnv = "WHATSAPP_SIGNAL_SECRET"
	signalHeaderEnv = "WHATSAPP_SIGNAL_HEADER"

	defaultSignalHeader  = "X-Whatsapp-Signal-Secret"
	signalAttemptTimeout = 3 * time.Second
	// Upper bound on deliveries in flight. If the receiver is down during a
	// burst, further signals are dropped rather than piling up goroutines.
	maxSignalsInFlight = 32
)

// Waits before the 2nd and 3rd attempt (so: 1 try + 2 retries).
var defaultSignalBackoff = []time.Duration{500 * time.Millisecond, 2 * time.Second}

// messageSignal is the JSON body of a signal.
type messageSignal struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	Timestamp string `json:"timestamp"` // RFC 3339, UTC
	Direction string `json:"direction"` // "inbound" or "outbound"
	IsGroup   bool   `json:"is_group"`
}

type signalSender struct {
	url     string
	header  string
	secret  string
	client  *http.Client
	backoff []time.Duration
	slots   chan struct{}
	logf    func(format string, args ...interface{})
}

// messageSignals is nil when signals are not configured; Notify on a nil
// sender is a no-op, so call sites don't need to check.
var messageSignals *signalSender

// newSignalSenderFromEnv returns nil, after logging once, unless both the URL
// and the secret are configured.
func newSignalSenderFromEnv(logf func(format string, args ...interface{})) *signalSender {
	url := strings.TrimSpace(os.Getenv(signalURLEnv))
	secret := strings.TrimSpace(os.Getenv(signalSecretEnv))
	if url == "" || secret == "" {
		logf("Message signals are off (set %s and %s to enable)", signalURLEnv, signalSecretEnv)
		return nil
	}
	header := strings.TrimSpace(os.Getenv(signalHeaderEnv))
	if header == "" {
		header = defaultSignalHeader
	}
	logf("Message signals are on (secret sent in the %s header)", header)
	return &signalSender{
		url:     url,
		header:  header,
		secret:  secret,
		client:  &http.Client{},
		backoff: defaultSignalBackoff,
		slots:   make(chan struct{}, maxSignalsInFlight),
		logf:    logf,
	}
}

// newMessageSignal builds the signal for a live message event. chat is the
// resolved (phone-based) chat JID the message was stored under. ok is false
// for messages that must not be signalled.
func newMessageSignal(msg *events.Message, chat types.JID) (sig messageSignal, ok bool) {
	if chat.Server == types.BroadcastServer || msg.Info.Chat.Server == types.BroadcastServer {
		// status@broadcast and broadcast lists are not conversations.
		return messageSignal{}, false
	}
	direction := "inbound"
	if msg.Info.IsFromMe {
		direction = "outbound"
	}
	return messageSignal{
		ChatJID:   chat.String(),
		MessageID: msg.Info.ID,
		Timestamp: msg.Info.Timestamp.UTC().Format(time.RFC3339),
		Direction: direction,
		IsGroup:   chat.Server == types.GroupServer,
	}, true
}

// Notify delivers sig in the background and returns immediately. It never
// blocks the caller; delivery failures are only logged.
func (s *signalSender) Notify(sig messageSignal) {
	if s == nil {
		return
	}
	select {
	case s.slots <- struct{}{}:
	default:
		s.logf("Message signal dropped: %d deliveries already in flight", cap(s.slots))
		return
	}
	go func() {
		defer func() { <-s.slots }()
		s.deliver(sig)
	}()
}

func (s *signalSender) deliver(sig messageSignal) {
	body, err := json.Marshal(sig)
	if err != nil {
		s.logf("Message signal not sent: %v", err)
		return
	}
	attempts := len(s.backoff) + 1
	for attempt := 1; ; attempt++ {
		retry, err := s.post(body)
		if err == nil {
			return
		}
		if !retry || attempt == attempts {
			// Deliberately no chat, sender or content in this line.
			s.logf("Message signal failed after %d attempt(s): %v", attempt, err)
			return
		}
		time.Sleep(s.backoff[attempt-1])
	}
}

// post makes one attempt. retry reports whether the failure is worth retrying
// (network error, timeout, 429 or 5xx).
func (s *signalSender) post(body []byte) (retry bool, err error) {
	ctx, cancel := context.WithTimeout(context.Background(), signalAttemptTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(body))
	if err != nil {
		return false, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(s.header, s.secret)

	resp, err := s.client.Do(req)
	if err != nil {
		return true, err
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))

	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return false, nil
	case resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500:
		return true, fmt.Errorf("receiver returned HTTP %d", resp.StatusCode)
	default:
		return false, fmt.Errorf("receiver returned HTTP %d", resp.StatusCode)
	}
}
