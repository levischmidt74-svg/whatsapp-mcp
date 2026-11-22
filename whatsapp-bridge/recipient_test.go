package main

import "testing"

func TestParseRecipient(t *testing.T) {
	cases := map[string]string{
		"13375550100":                  "13375550100@s.whatsapp.net",
		"+13375550100":                 "13375550100@s.whatsapp.net",
		" +1 (337) 555-0100 ":          "13375550100@s.whatsapp.net",
		"+13375550100@s.whatsapp.net":  "13375550100@s.whatsapp.net",
		"13375550100@s.whatsapp.net":   "13375550100@s.whatsapp.net",
		"120363000000000001@g.us":      "120363000000000001@g.us",
		"13375550100-1577456132@g.us":  "13375550100-1577456132@g.us",
		"185366493536339@lid":          "185366493536339@lid",
		"185366493536339:12@lid":       "185366493536339:12@lid",
		"13375550100:3@s.whatsapp.net": "13375550100:3@s.whatsapp.net",
	}
	for input, want := range cases {
		got, err := parseRecipient(input)
		if err != nil {
			t.Errorf("parseRecipient(%q) failed: %v", input, err)
			continue
		}
		if got.String() != want {
			t.Errorf("parseRecipient(%q) = %q, want %q", input, got.String(), want)
		}
	}

	for _, bad := range []string{"", "   ", "+", "not a number"} {
		if got, err := parseRecipient(bad); err == nil {
			t.Errorf("parseRecipient(%q) = %q, want an error", bad, got.String())
		}
	}
}
