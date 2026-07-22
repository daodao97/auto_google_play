package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMockRedeemLifecycle(t *testing.T) {
	handler := newMockRedeemServer("test-key")
	request := func(method, path, body, key string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+key)
		req.Header.Set("Content-Type", "application/json")
		resp := httptest.NewRecorder()
		handler.ServeHTTP(resp, req)
		return resp
	}
	if response := request(http.MethodPost, "/api/redeem/tasks", `{}`, "wrong"); response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized status=%d body=%s", response.Code, response.Body.String())
	}
	created := request(http.MethodPost, "/api/redeem/tasks", `{"sku":"pro","channel":"official","session":"{\"accessToken\":\"mock\"}"}`, "test-key")
	if created.Code != http.StatusOK {
		t.Fatalf("create status=%d body=%s", created.Code, created.Body.String())
	}
	var payload struct {
		Data struct{ TaskID, Status string } `json:"data"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &payload); err != nil || !strings.HasPrefix(payload.Data.TaskID, "rdm_mock_") || payload.Data.Status != "pending" {
		t.Fatalf("create payload=%s err=%v", created.Body.String(), err)
	}
	want := []string{"pending", "processing", "success"}
	for index, status := range want {
		response := request(http.MethodGet, "/api/redeem/tasks/"+payload.Data.TaskID, "", "test-key")
		var result struct {
			Data struct{ Status string } `json:"data"`
		}
		if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil || result.Data.Status != status {
			t.Fatalf("poll %d status=%d body=%s err=%v", index+1, response.Code, response.Body.String(), err)
		}
	}

	failed := request(http.MethodPost, "/api/redeem/tasks", `{"sku":"plus","channel":"official","session":"{\"accessToken\":\"mock\",\"mockResult\":\"failed\"}"}`, "test-key")
	if failed.Code != http.StatusOK {
		t.Fatalf("create failed task status=%d body=%s", failed.Code, failed.Body.String())
	}
	if err := json.Unmarshal(failed.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode failed task: %v", err)
	}
	for range 3 {
		failed = request(http.MethodGet, "/api/redeem/tasks/"+payload.Data.TaskID, "", "test-key")
	}
	var failedPayload struct {
		Data struct {
			Status string `json:"status"`
			Error  struct {
				Code string `json:"code"`
			} `json:"error"`
		} `json:"data"`
	}
	if err := json.Unmarshal(failed.Body.Bytes(), &failedPayload); err != nil || failedPayload.Data.Status != "failed" || failedPayload.Data.Error.Code != "MOCK_UPGRADE_FAILED" {
		t.Fatalf("failed terminal body=%s err=%v", failed.Body.String(), err)
	}
}
