package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

type mockTask struct {
	ID         string
	SKU        string
	Channel    string
	Fail       bool
	PollCount  int
	CreatedAt  time.Time
	UpdatedAt  time.Time
	FinishedAt *time.Time
}

type mockRedeemServer struct {
	apiKey string
	mu     sync.Mutex
	tasks  map[string]*mockTask
}

func main() {
	bind := env("MOCK_REDEEM_BIND", "127.0.0.1:4100")
	apiKey := env("MOCK_REDEEM_API_KEY", "local-mock-key")
	log.Printf("mock ChatGPT redeem API listening on %s", bind)
	if err := http.ListenAndServe(bind, newMockRedeemServer(apiKey)); err != nil {
		log.Fatal(err)
	}
}

func env(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func newMockRedeemServer(apiKey string) http.Handler {
	s := &mockRedeemServer{apiKey: apiKey, tasks: map[string]*mockTask{}}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/redeem/tasks", s.createTask)
	mux.HandleFunc("/api/redeem/tasks/", s.getTask)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
	})
	return mux
}

func (s *mockRedeemServer) authorized(r *http.Request) bool {
	return r.Header.Get("Authorization") == "Bearer "+s.apiKey
}

func (s *mockRedeemServer) createTask(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "method not allowed")
		return
	}
	if !s.authorized(r) {
		writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "API Key is missing or invalid")
		return
	}
	var req struct {
		SKU     string `json:"sku"`
		Channel string `json:"channel"`
		Session string `json:"session"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	if err := decoder.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "INVALID_PARAMETER", "request body must be valid JSON")
		return
	}
	if req.SKU != "plus" && req.SKU != "pro" && req.SKU != "prolite" {
		writeError(w, http.StatusBadRequest, "INVALID_PARAMETER", "sku must be one of: plus, pro, prolite")
		return
	}
	if strings.TrimSpace(req.Channel) == "" {
		writeError(w, http.StatusBadRequest, "INVALID_PARAMETER", "channel is required")
		return
	}
	var session map[string]any
	if req.Session == "" || json.Unmarshal([]byte(req.Session), &session) != nil {
		writeError(w, http.StatusBadRequest, "INVALID_PARAMETER", "session must be a valid JSON string")
		return
	}
	id := "rdm_mock_" + randomHex(13)
	now := time.Now().UTC()
	task := &mockTask{ID: id, SKU: req.SKU, Channel: req.Channel, Fail: session["mockResult"] == "failed", CreatedAt: now, UpdatedAt: now}
	s.mu.Lock()
	s.tasks[id] = task
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "data": taskData(task)})
}

func (s *mockRedeemServer) getTask(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "method not allowed")
		return
	}
	if !s.authorized(r) {
		writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "API Key is missing or invalid")
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/api/redeem/tasks/")
	s.mu.Lock()
	task, exists := s.tasks[id]
	if exists {
		task.PollCount++
		task.UpdatedAt = time.Now().UTC()
		if task.PollCount >= 3 && task.FinishedAt == nil {
			finished := task.UpdatedAt
			task.FinishedAt = &finished
		}
	}
	var data map[string]any
	if exists {
		data = taskData(task)
	}
	s.mu.Unlock()
	if !exists {
		writeError(w, http.StatusNotFound, "TASK_NOT_FOUND", "Task not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "data": data})
}

func taskData(task *mockTask) map[string]any {
	status := "pending"
	if task.PollCount == 2 {
		status = "processing"
	} else if task.PollCount >= 3 {
		if task.Fail {
			status = "failed"
		} else {
			status = "success"
		}
	}
	data := map[string]any{"taskId": task.ID, "status": status, "createdAt": task.CreatedAt, "updatedAt": task.UpdatedAt}
	if status == "success" {
		data["result"] = map[string]any{"sku": task.SKU, "channel": task.Channel, "message": "Account upgraded successfully (mock)"}
	}
	if status == "failed" {
		data["error"] = map[string]any{"code": "MOCK_UPGRADE_FAILED", "message": "Account upgrade failed by mock request"}
	}
	if task.FinishedAt != nil {
		data["finishedAt"] = task.FinishedAt
	}
	return data
}

func randomHex(bytes int) string {
	b := make([]byte, bytes)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{"success": false, "error": map[string]any{"code": code, "message": message}})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
