package main

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestDescribeSendError(t *testing.T) {
	timedOut := describeSendError(fmt.Errorf("failed to get device list: %w", context.DeadlineExceeded), 40*time.Second)
	for _, want := range []string{"did not confirm", "40s", "may or may not have been delivered", "failed to get device list"} {
		if !strings.Contains(timedOut, want) {
			t.Errorf("timeout message %q should mention %q", timedOut, want)
		}
	}

	other := describeSendError(errors.New("server returned error 463"), 40*time.Second)
	if other != "Error sending message: server returned error 463" {
		t.Errorf("unexpected message for a plain error: %q", other)
	}
}

func TestSendLimitsFitInsideServerWriteTimeout(t *testing.T) {
	if total := lidLookupTimeout + sendTimeout; total >= 60*time.Second {
		t.Errorf("send limits total %v, must stay under the 60s REST write timeout", total)
	}
}
