package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

const testSignalSecret = "test-shared-secret"

type capturedSignal struct {
	body   []byte
	header http.Header
}

// useSignalReceiver points messageSignals at handler for the duration of the
// test, with near-zero retry backoff. It also parks the legacy content
// webhook on a local sink so handleMessage doesn't wait on a closed port.
func useSignalReceiver(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)

	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	t.Cleanup(sink.Close)
	t.Setenv("WEBHOOK_URL", sink.URL)

	t.Setenv(signalURLEnv, srv.URL+"/hook")
	t.Setenv(signalSecretEnv, testSignalSecret)
	sender := newSignalSenderFromEnv(func(string, ...interface{}) {})
	if sender == nil {
		t.Fatal("expected signals to be enabled")
	}
	sender.backoff = []time.Duration{time.Millisecond, time.Millisecond}

	previous := messageSignals
	messageSignals = sender
	t.Cleanup(func() { messageSignals = previous })
}

// captureSignals records every signal request and answers 200.
func captureSignals(t *testing.T) <-chan capturedSignal {
	t.Helper()
	ch := make(chan capturedSignal, 16)
	useSignalReceiver(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		ch <- capturedSignal{body: body, header: r.Header.Clone()}
	})
	return ch
}

func expectNoSignal(t *testing.T, ch <-chan capturedSignal, what string) {
	t.Helper()
	select {
	case got := <-ch:
		t.Fatalf("%s produced a signal: %s", what, got.body)
	case <-time.After(300 * time.Millisecond):
	}
}

func TestMessageSignal_InboundMessageSendsOneSignalWithoutContent(t *testing.T) {
	signals := captureSignals(t)
	client := newTestClient(&mockLIDStore{})
	ms := newTestMessageStore(t)

	const text = "Top secret message body"
	msg := buildTextMessage(phonePN, phonePN, types.EmptyJID, types.EmptyJID, false, text)
	msg.Info.PushName = "Sender Display Name"
	msg.Info.Timestamp = time.Date(2026, 11, 21, 12, 30, 45, 0, time.FixedZone("CST", -6*3600))

	handleMessage(client, ms, msg, testLogger())

	var got capturedSignal
	select {
	case got = <-signals:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for the signal")
	}
	expectNoSignal(t, signals, "a single message (second signal)")

	var fields map[string]interface{}
	if err := json.Unmarshal(got.body, &fields); err != nil {
		t.Fatalf("signal body is not JSON: %v", err)
	}
	want := map[string]interface{}{
		"chat_jid":   phonePN.String(),
		"message_id": "test-msg-001",
		"timestamp":  "2026-11-21T18:30:45Z",
		"direction":  "inbound",
		"is_group":   false,
	}
	if len(fields) != len(want) {
		t.Errorf("signal has unexpected fields: %s", got.body)
	}
	for key, value := range want {
		if fields[key] != value {
			t.Errorf("signal %s = %v, want %v", key, fields[key], value)
		}
	}
	for _, private := range []string{text, "Sender Display Name"} {
		if strings.Contains(string(got.body), private) {
			t.Errorf("signal leaks %q: %s", private, got.body)
		}
	}
	if secret := got.header.Get(defaultSignalHeader); secret != testSignalSecret {
		t.Errorf("%s header = %q, want the shared secret", defaultSignalHeader, secret)
	}
	if count := queryMessageCount(ms, phonePN.String()); count != 1 {
		t.Errorf("expected the message to be stored, got %d rows", count)
	}
}

func TestMessageSignal_OutboundGroupMessage(t *testing.T) {
	signals := captureSignals(t)
	client := newTestClientWithSelf(&mockLIDStore{}, selfPhone)
	ms := newTestMessageStore(t)
	// Pre-name the chat so the handler doesn't ask WhatsApp for group info.
	group := types.JID{User: "120363000000000001", Server: types.GroupServer}
	if err := ms.StoreChat(group.String(), "Test group", time.Now()); err != nil {
		t.Fatalf("failed to seed chat: %v", err)
	}

	msg := buildTextMessage(group, selfPhone, types.EmptyJID, types.EmptyJID, true, "sent from my phone")
	msg.Info.IsGroup = true
	handleMessage(client, ms, msg, testLogger())

	select {
	case got := <-signals:
		var sig messageSignal
		if err := json.Unmarshal(got.body, &sig); err != nil {
			t.Fatalf("signal body is not JSON: %v", err)
		}
		if sig.Direction != "outbound" || !sig.IsGroup || sig.ChatJID != group.String() {
			t.Errorf("unexpected signal: %+v", sig)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for the signal")
	}
}

func TestMessageSignal_NotSentForHistoryReceiptsPresenceOrStatus(t *testing.T) {
	signals := captureSignals(t)
	client := newTestClientWithSelf(&mockLIDStore{}, selfPhone)
	ms := newTestMessageStore(t)
	logger := testLogger()

	historySync := &events.HistorySync{
		Data: &waProto.HistorySync{
			SyncType: waProto.HistorySync_RECENT.Enum(),
			Conversations: []*waProto.Conversation{{
				ID: proto.String(phonePN.String()),
				Messages: []*waProto.HistorySyncMsg{{
					Message: &waProto.WebMessageInfo{
						Key: &waCommon.MessageKey{
							ID:     proto.String("hist-msg-001"),
							FromMe: proto.Bool(false),
						},
						MessageTimestamp: proto.Uint64(uint64(time.Now().Unix())),
						Message:          &waProto.Message{Conversation: proto.String("backfilled text")},
					},
				}},
			}},
		},
	}
	if !handleStoreEvent(client, ms, historySync, logger) {
		t.Fatal("history sync should be handled")
	}
	if count := queryMessageCount(ms, phonePN.String()); count != 1 {
		t.Fatalf("expected the history message to be stored, got %d rows", count)
	}
	expectNoSignal(t, signals, "history sync")

	for name, evt := range map[string]interface{}{
		"receipt":       &events.Receipt{MessageIDs: []types.MessageID{"test-msg-001"}, Type: types.ReceiptTypeRead},
		"presence":      &events.Presence{From: phonePN},
		"chat presence": &events.ChatPresence{},
	} {
		if handleStoreEvent(client, ms, evt, logger) {
			t.Errorf("%s should not be handled as a store event", name)
		}
		expectNoSignal(t, signals, name)
	}

	status := buildTextMessage(types.StatusBroadcastJID, phonePN, types.EmptyJID, types.EmptyJID, false, "status update")
	handleStoreEvent(client, ms, status, logger)
	if count := queryMessageCount(ms, types.StatusBroadcastJID.String()); count != 1 {
		t.Fatalf("expected the status message to be stored as before, got %d rows", count)
	}
	expectNoSignal(t, signals, "status@broadcast")
}

func TestMessageSignal_ReceiverErrorDoesNotBlockHandler(t *testing.T) {
	var requests atomic.Int32
	done := make(chan struct{})
	useSignalReceiver(t, func(w http.ResponseWriter, r *http.Request) {
		if requests.Add(1) == 3 {
			defer close(done)
		}
		w.WriteHeader(http.StatusInternalServerError)
	})
	client := newTestClient(&mockLIDStore{})
	ms := newTestMessageStore(t)

	start := time.Now()
	handleMessage(client, ms, buildTextMessage(phonePN, phonePN, types.EmptyJID, types.EmptyJID, false, "hello"), testLogger())
	if elapsed := time.Since(start); elapsed > 500*time.Millisecond {
		t.Errorf("handler took %v with a failing receiver", elapsed)
	}
	if count := queryMessageCount(ms, phonePN.String()); count != 1 {
		t.Errorf("expected the message to be stored, got %d rows", count)
	}

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatalf("expected 3 attempts (1 + 2 retries), saw %d", requests.Load())
	}
	time.Sleep(100 * time.Millisecond)
	if n := requests.Load(); n != 3 {
		t.Errorf("expected exactly 3 attempts, saw %d", n)
	}
}

func TestMessageSignal_ReceiverTimeoutDoesNotBlockHandler(t *testing.T) {
	release := make(chan struct{})
	arrived := make(chan struct{}, 4)
	useSignalReceiver(t, func(w http.ResponseWriter, r *http.Request) {
		arrived <- struct{}{}
		select { // hang like an unresponsive receiver
		case <-release:
		case <-r.Context().Done():
		}
	})
	t.Cleanup(func() { close(release) })
	client := newTestClient(&mockLIDStore{})
	ms := newTestMessageStore(t)

	start := time.Now()
	handleMessage(client, ms, buildTextMessage(phonePN, phonePN, types.EmptyJID, types.EmptyJID, false, "hello"), testLogger())
	if elapsed := time.Since(start); elapsed > 500*time.Millisecond {
		t.Errorf("handler took %v with a hanging receiver", elapsed)
	}
	if count := queryMessageCount(ms, phonePN.String()); count != 1 {
		t.Errorf("expected the message to be stored, got %d rows", count)
	}
	select {
	case <-arrived:
	case <-time.After(2 * time.Second):
		t.Fatal("the signal was never attempted")
	}
}

func TestMessageSignal_OffWithoutConfiguration(t *testing.T) {
	for name, env := range map[string][2]string{
		"nothing set": {"", ""},
		"only url":    {"http://127.0.0.1:1/hook", ""},
		"only secret": {"", "s3cret"},
	} {
		t.Setenv(signalURLEnv, env[0])
		t.Setenv(signalSecretEnv, env[1])
		var logged []string
		sender := newSignalSenderFromEnv(func(format string, args ...interface{}) { logged = append(logged, format) })
		if sender != nil {
			t.Errorf("%s: expected signals to be off", name)
		}
		if len(logged) != 1 {
			t.Errorf("%s: expected one log line, got %d", name, len(logged))
		}
	}

	// A nil sender must be safe to use.
	var off *signalSender
	off.Notify(messageSignal{MessageID: "x"})
}
