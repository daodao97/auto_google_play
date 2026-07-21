package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestExtractCodeMatchesReference(t *testing.T) {
	code, ok := extractCode("GOOGLE BMR 123456", "b-m-r")
	if !ok || code != "123456" {
		t.Fatalf("unexpected match %q %v", code, ok)
	}
	if _, ok = extractCode("GOOGLE XYZ 123456", "BMR"); ok {
		t.Fatal("matched wrong reference")
	}
}
func TestPickLatestCode(t *testing.T) {
	loc := time.FixedZone("CST", 8*3600)
	records := []qbitRecord{{CardID: "card-1", Detail: "GOOGLE BMR 111111", TransactionTime: "2026-07-18 10:00:00"}, {CardID: "card-1", Detail: "GOOGLE BMR 222222", TransactionTime: "2026-07-18 10:01:00"}}
	code, ok := pickLatestCode(records, "card-1", "BMR", loc)
	if !ok || code != "222222" {
		t.Fatalf("unexpected code %q %v", code, ok)
	}
}

func TestQuerySlashVerifyCode(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/card/card_123/events" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("X-API-Key"); got != "slash-key" {
			t.Fatalf("X-API-Key = %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"items":[
			{"createdAt":"2026-07-18T10:00:00Z","merchantData":{"description":"GOOGLE BMR 111111"}},
			{"createdAt":"2026-07-18T10:01:00Z","merchant":{"description":"GOOGLE BMR 654321"}}
		]}`)
	}))
	defer server.Close()

	code, found, err := querySlashVerifyCode(context.Background(), server.URL, " slash-key ", "card_123", "BMR", now)
	if err != nil || !found || code != "654321" {
		t.Fatalf("querySlashVerifyCode = %q, %v, %v", code, found, err)
	}
}

func TestQuerySlashVerifyCodePendingAndError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/card/fail/events" {
			http.Error(w, "unavailable", http.StatusBadGateway)
			return
		}
		_, _ = fmt.Fprint(w, `{"events":[]}`)
	}))
	defer server.Close()

	_, found, err := querySlashVerifyCode(context.Background(), server.URL, "key", "card", "BMR", time.Now())
	if err != nil || found {
		t.Fatalf("pending query = %v, %v", found, err)
	}
	if _, _, err = querySlashVerifyCode(context.Background(), server.URL, "key", "fail", "BMR", time.Now()); err == nil {
		t.Fatal("expected upstream status error")
	}
}

func TestFetchQbitCardHistoryReturnsRawResponse(t *testing.T) {
	const upstreamRaw = `{"code":200,"data":{"records":[{"detail":"DECLINED"},{"detail":"GOOGLE BMR 123456"}]}}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/quantum/card/budget-card/transaction/page" {
			t.Fatalf("history path = %s", r.URL.Path)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if _, hasGroupFilter := payload["groupIds"]; hasGroupFilter {
			t.Fatalf("history request unexpectedly filters by group: %#v", payload)
		}
		if payload["size"] != float64(qbitTransactionPageSize) {
			t.Fatalf("history request size = %#v", payload["size"])
		}
		if window, ok := payload["transactionTime"].([]any); !ok || len(window) != 2 {
			t.Fatalf("history request transactionTime = %#v", payload["transactionTime"])
		}
		cardIDs, _ := payload["cardIds"].([]any)
		if len(cardIDs) != 1 || cardIDs[0] != "card-raw" {
			t.Fatalf("history request cardIds = %#v", payload["cardIds"])
		}
		_, _ = fmt.Fprint(w, upstreamRaw)
	}))
	defer server.Close()

	raw, err := fetchQbitCardHistory(context.Background(), server.URL, "token", "", "card-raw")
	if err != nil || string(raw) != upstreamRaw {
		t.Fatalf("raw response = %q, err=%v", raw, err)
	}
}
