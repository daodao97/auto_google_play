package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

const qbitBaseURL = "https://assets-prod.interlace.money"
const slashBaseURL = "https://api.slash.com"
const qbitTransactionPageSize = 200

var sixDigitCode = regexp.MustCompile(`\d{6}`)

type qbitRecord struct {
	ID              string `json:"id"`
	TransactionTime string `json:"transactionTime"`
	CardID          string `json:"cardId"`
	Detail          string `json:"detail"`
}
type qbitResponse struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    *struct {
		Records []qbitRecord `json:"records"`
	} `json:"data"`
}

func (s *Server) verifyCode(c *gin.Context) {
	var req struct {
		CardPoolID int64  `json:"cardPoolId"`
		GoogleRef  string `json:"googleRef"`
	}
	if !bind(c, &req) {
		return
	}
	if req.CardPoolID <= 0 || strings.TrimSpace(req.GoogleRef) == "" {
		fail(c, 400, "BAD_REQUEST", errors.New("positive cardPoolId and googleRef are required"))
		return
	}
	card, err := s.store.CardByIDForAPIKey(c.Request.Context(), req.CardPoolID, currentAPIKey(c).ID)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	source := strings.ToLower(strings.TrimSpace(card.Source))
	if !strings.HasPrefix(source, "qbit") && !strings.HasPrefix(source, "slash") {
		fail(c, 400, "UNSUPPORTED_SOURCE", errors.New("unsupported card source"))
		return
	}
	token, err := s.store.Credential(c.Request.Context(), source)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	var code string
	var found bool
	if strings.HasPrefix(source, "slash") {
		code, found, err = querySlashVerifyCode(c.Request.Context(), s.slashBaseURL, token, card.CardID, req.GoogleRef, time.Now())
	} else {
		code, found, err = queryQbitVerifyCode(c.Request.Context(), s.qbitBaseURL, token, c.GetHeader("Fingerprint"), card.CardID, req.GoogleRef, time.Now())
	}
	if err != nil {
		fail(c, 502, "UPSTREAM_ERROR", err)
		return
	}
	if !found {
		ok(c, gin.H{"status": "pending", "message": "等待验证码"})
		return
	}
	ok(c, gin.H{"status": "ok", "code": code})
}

func (s *Server) cardHistory(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, http.StatusBadRequest, "BAD_ID", err)
		return
	}
	card, err := s.store.CardByID(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	source := strings.ToLower(strings.TrimSpace(card.Source))
	if !strings.HasPrefix(source, "qbit") && !strings.HasPrefix(source, "slash") {
		fail(c, http.StatusBadRequest, "UNSUPPORTED_SOURCE", errors.New("unsupported card source"))
		return
	}
	token, err := s.store.Credential(c.Request.Context(), source)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	var raw []byte
	if strings.HasPrefix(source, "slash") {
		raw, err = fetchSlashCardEvents(c.Request.Context(), s.slashBaseURL, token, card.CardID)
	} else {
		raw, err = fetchQbitCardHistory(c.Request.Context(), s.qbitBaseURL, token, c.GetHeader("Fingerprint"), card.CardID)
	}
	if err != nil {
		fail(c, http.StatusBadGateway, "UPSTREAM_ERROR", err)
		return
	}
	ok(c, gin.H{
		"cardPoolId": card.ID,
		"source":     card.Source,
		"cardId":     card.CardID,
		"raw":        string(raw),
	})
}

func querySlashVerifyCode(ctx context.Context, baseURL, apiKey, cardID, googleRef string, now time.Time) (string, bool, error) {
	raw, err := fetchSlashCardEvents(ctx, baseURL, apiKey, cardID)
	if err != nil {
		return "", false, err
	}
	var payload any
	if err = json.Unmarshal(raw, &payload); err != nil {
		return "", false, err
	}
	code, found := pickLatestSlashCode(slashEventItems(payload), googleRef, now)
	return code, found, nil
}

func fetchSlashCardEvents(ctx context.Context, baseURL, apiKey, cardID string) ([]byte, error) {
	path := "/card/" + url.PathEscape(strings.TrimSpace(cardID)) + "/events"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-API-Key", strings.TrimSpace(apiKey))
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("slash http status %d", resp.StatusCode)
	}
	return raw, nil
}

func slashEventItems(payload any) []any {
	switch value := payload.(type) {
	case []any:
		return value
	case map[string]any:
		for _, key := range []string{"items", "events", "authorizationEvents", "records", "data"} {
			if nested, ok := value[key]; ok {
				if items := slashEventItems(nested); len(items) > 0 {
					return items
				}
			}
		}
		return []any{value}
	default:
		return nil
	}
}

func collectJSONStrings(value any, values *[]string) {
	switch item := value.(type) {
	case string:
		*values = append(*values, item)
	case []any:
		for _, child := range item {
			collectJSONStrings(child, values)
		}
	case map[string]any:
		for _, child := range item {
			collectJSONStrings(child, values)
		}
	}
}

func slashEventCode(event any, googleRef string) (string, bool) {
	var values []string
	collectJSONStrings(event, &values)
	for _, value := range values {
		if code, ok := extractCode(value, googleRef); ok {
			return code, true
		}
	}
	return "", false
}

func slashEventTime(value any) time.Time {
	object, ok := value.(map[string]any)
	if !ok {
		return time.Time{}
	}
	for _, key := range []string{"authorizedAt", "createdAt", "occurredAt", "timestamp", "date"} {
		raw, exists := object[key]
		if !exists {
			continue
		}
		switch v := raw.(type) {
		case string:
			if parsed, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(v)); err == nil {
				return parsed
			}
			if millis, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64); err == nil {
				return time.UnixMilli(millis)
			}
		case float64:
			return time.UnixMilli(int64(v))
		}
	}
	return time.Time{}
}

func pickLatestSlashCode(events []any, googleRef string, now time.Time) (string, bool) {
	var result string
	var latest time.Time
	windowStart, _ := qbitWindow(now)
	for _, event := range events {
		code, ok := slashEventCode(event, googleRef)
		if !ok {
			continue
		}
		at := slashEventTime(event)
		if !at.IsZero() && at.Before(time.UnixMilli(windowStart)) {
			continue
		}
		if result == "" || !at.IsZero() && (latest.IsZero() || at.After(latest)) {
			result = code
			latest = at
		}
	}
	return result, result != ""
}

func queryQbitVerifyCode(ctx context.Context, baseURL, token, fingerprint, cardID, googleRef string, now time.Time) (string, bool, error) {
	start, end := qbitWindow(now)
	payload := map[string]any{"current": 1, "size": qbitTransactionPageSize, "cardIds": []string{strings.TrimSpace(cardID)}, "transactionTime": []int64{start, end}}
	raw, err := fetchQbitCardHistoryWithPayload(ctx, baseURL, token, fingerprint, payload)
	if err != nil {
		return "", false, err
	}
	var result qbitResponse
	if err = json.Unmarshal(raw, &result); err != nil {
		return "", false, err
	}
	if result.Code != 200 {
		return "", false, fmt.Errorf("qbit error %d: %s", result.Code, result.Message)
	}
	if result.Data == nil {
		return "", false, nil
	}
	code, found := pickLatestCode(result.Data.Records, cardID, googleRef, now.Location())
	return code, found, nil
}

func fetchQbitCardHistory(ctx context.Context, baseURL, token, fingerprint, cardID string) ([]byte, error) {
	start, end := qbitHistoryWindow(time.Now())
	payload := map[string]any{
		"current":         1,
		"size":            qbitTransactionPageSize,
		"cardIds":         []string{strings.TrimSpace(cardID)},
		"transactionTime": []int64{start, end},
	}
	return fetchQbitCardHistoryWithPayload(ctx, baseURL, token, fingerprint, payload)
}

func fetchQbitCardHistoryWithPayload(ctx context.Context, baseURL, token, fingerprint string, payload map[string]any) ([]byte, error) {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(baseURL, "/")+"/api/quantum/card/budget-card/transaction/page", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(token))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Lang", "zh_CN")
	req.Header.Set("Origin", "https://www.interlace.money")
	req.Header.Set("Referer", "https://www.interlace.money/")
	req.Header.Set("Systemtype", "QbitInternational")
	req.Header.Set("Website-Version", "business")
	if fingerprint != "" {
		req.Header.Set("Fingerprint", fingerprint)
	}
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("qbit http status %d", resp.StatusCode)
	}
	return raw, nil
}
func qbitWindow(now time.Time) (int64, int64) {
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, now.Location())
	return today.AddDate(0, 0, -1).UnixMilli(), today.AddDate(0, 0, 1).Add(-time.Millisecond).UnixMilli()
}

func qbitHistoryWindow(now time.Time) (int64, int64) {
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, now.Location())
	return today.AddDate(0, -1, 0).UnixMilli(), today.AddDate(0, 0, 1).Add(-time.Millisecond).UnixMilli()
}
func normalizeMatch(value string) string {
	value = strings.ToUpper(strings.TrimSpace(value))
	r := strings.NewReplacer("\t", " ", "\n", " ", "\r", " ", "*", " ", "_", " ", "-", " ")
	return strings.Join(strings.Fields(r.Replace(value)), " ")
}
func extractCode(detail, googleRef string) (string, bool) {
	fields := strings.Fields(normalizeMatch(detail))
	target := strings.ReplaceAll(normalizeMatch(googleRef), " ", "")
	for i := 0; i+2 < len(fields); i++ {
		if fields[i] != "GOOGLE" {
			continue
		}
		code := sixDigitCode.FindString(fields[i+2])
		if code == fields[i+2] && (target == "" || strings.ReplaceAll(fields[i+1], " ", "") == target) {
			return code, true
		}
	}
	return "", false
}
func pickLatestCode(records []qbitRecord, cardID, ref string, loc *time.Location) (string, bool) {
	if loc == nil {
		loc = time.Local
	}
	var result string
	var latest time.Time
	for _, record := range records {
		if strings.TrimSpace(cardID) != "" && strings.TrimSpace(record.CardID) != strings.TrimSpace(cardID) {
			continue
		}
		code, ok := extractCode(record.Detail, ref)
		if !ok {
			continue
		}
		at, err := time.ParseInLocation("2006-01-02 15:04:05", strings.TrimSpace(record.TransactionTime), loc)
		if result == "" || err == nil && at.After(latest) {
			result = code
			if err == nil {
				latest = at
			}
		}
	}
	return result, result != ""
}
