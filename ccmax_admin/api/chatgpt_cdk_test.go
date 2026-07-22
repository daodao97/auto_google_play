package api

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"ccmax/conf"
	"ccmax/dao"

	"github.com/gin-gonic/gin"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func testChatGPTSession(email string) string {
	claims, _ := json.Marshal(map[string]any{"https://api.openai.com/profile": map[string]any{"email": email}})
	token := "e30." + base64.RawURLEncoding.EncodeToString(claims) + ".signature"
	session, _ := json.Marshal(map[string]any{"accessToken": token, "userId": "test-user"})
	return string(session)
}

func redeemBody(code, session string) string {
	return fmt.Sprintf(`{"code":%q,"channel":"official","session":%q}`, code, session)
}

func TestChatGPTCDKLifecycle(t *testing.T) {
	var createCalls atomic.Int32
	session := testChatGPTSession("lifecycle@example.com")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer upstream-secret" {
			t.Fatalf("missing upstream authorization: %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/redeem/tasks":
			createCalls.Add(1)
			var body struct{ SKU, Channel, Session string }
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if body.SKU != "pro" || body.Channel != "official" || body.Session != session {
				t.Fatalf("unexpected upstream request: %#v", body)
			}
			fmt.Fprint(w, `{"success":true,"data":{"taskId":"rdm_test_1","status":"pending","createdAt":"2026-07-21T11:30:00Z"}}`)
		case r.Method == http.MethodGet && r.URL.Path == "/api/redeem/tasks/rdm_test_1":
			fmt.Fprint(w, `{"success":true,"data":{"taskId":"rdm_test_1","status":"success","result":{"sku":"pro","channel":"official","message":"Account upgraded successfully"}}}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()

	gin.SetMode(gin.TestMode)
	store, err := dao.Open(filepath.Join(t.TempDir(), "cdk.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	cfg := conf.Config{BootstrapUser: "root", BootstrapPass: "RootPass123", SessionTTL: time.Hour, ChatGPTRedeemURL: upstream.URL, ChatGPTRedeemKey: "upstream-secret"}
	server := New(store, cfg)
	if err = server.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	admin, err := store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const apiToken = "ccm_chatgpt_cdk_test"
	if _, err = store.CreateAPIKey(t.Context(), "cdk-test", "ccm_chat", tokenHash(apiToken), admin.ID); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	server.Setup(router)
	cookie := loginForTest(t, router, "root", "RootPass123")

	generateReq := httptest.NewRequest(http.MethodPost, "/api/admin/chatgpt-cdks/generate", strings.NewReader(`{"sku":"pro","quantity":2,"format":"uuid","remark":"customer batch"}`))
	generateReq.Header.Set("Content-Type", "application/json")
	generateReq.AddCookie(cookie)
	generateResp := httptest.NewRecorder()
	router.ServeHTTP(generateResp, generateReq)
	if generateResp.Code != http.StatusOK {
		t.Fatalf("generate status=%d body=%s", generateResp.Code, generateResp.Body.String())
	}
	var generated struct {
		Data struct {
			Items []dao.ChatGPTCDK `json:"items"`
			Count int              `json:"count"`
		} `json:"data"`
	}
	if err = json.Unmarshal(generateResp.Body.Bytes(), &generated); err != nil {
		t.Fatal(err)
	}
	if generated.Data.Count != 2 || len(generated.Data.Items) != 2 || len(generated.Data.Items[0].Code) != 36 {
		t.Fatalf("unexpected generate response: %s", generateResp.Body.String())
	}
	if !strings.HasPrefix(generated.Data.Items[0].OrderNo, "CDK-") || generated.Data.Items[0].Remark != "customer batch" {
		t.Fatalf("order number or remark missing: %s", generateResp.Body.String())
	}
	code := generated.Data.Items[0].Code

	check := requestWithAPIKey(t, router, http.MethodPost, "/api/chatgpt/cdk/check", apiToken, `{"code":"`+code+`"}`)
	if check.Code != http.StatusOK || !strings.Contains(check.Body.String(), `"available":true`) {
		t.Fatalf("check response: %d %s", check.Code, check.Body.String())
	}
	redeem := requestWithAPIKey(t, router, http.MethodPost, "/api/chatgpt/cdk/redeem", apiToken, redeemBody(code, session))
	if redeem.Code != http.StatusOK || strings.Contains(redeem.Body.String(), "rdm_test_1") {
		t.Fatalf("redeem response: %d %s", redeem.Code, redeem.Body.String())
	}
	var redeemResult struct {
		Data struct {
			TaskID string `json:"taskId"`
		} `json:"data"`
	}
	if err = json.Unmarshal(redeem.Body.Bytes(), &redeemResult); err != nil || !strings.HasPrefix(redeemResult.Data.TaskID, "ctk_") {
		t.Fatalf("missing local hash task id: %s err=%v", redeem.Body.String(), err)
	}
	if createCalls.Load() != 1 {
		t.Fatalf("create calls=%d", createCalls.Load())
	}
	retry := requestWithAPIKey(t, router, http.MethodPost, "/api/chatgpt/cdk/redeem", apiToken, redeemBody(code, session))
	if retry.Code != http.StatusConflict || createCalls.Load() != 1 {
		t.Fatalf("retry response: %d %s calls=%d", retry.Code, retry.Body.String(), createCalls.Load())
	}
	task := requestWithAPIKey(t, router, http.MethodGet, "/api/chatgpt/cdk/tasks/"+redeemResult.Data.TaskID, apiToken, "")
	if task.Code != http.StatusOK || !strings.Contains(task.Body.String(), `"status":"success"`) || strings.Contains(task.Body.String(), "rdm_test_1") {
		t.Fatalf("task response: %d %s", task.Code, task.Body.String())
	}
	stored, err := store.ChatGPTCDKByCode(t.Context(), code)
	if err != nil || !stored.Used || stored.TaskID == nil || stored.OrderNo != generated.Data.Items[0].OrderNo || stored.Remark != "customer batch" {
		t.Fatalf("stored cdk=%#v err=%v", stored, err)
	}
	localTask, err := store.ChatGPTTaskByID(t.Context(), *stored.TaskID)
	if err != nil || !strings.HasPrefix(localTask.HashID, "ctk_") || localTask.HashID != redeemResult.Data.TaskID || localTask.UserEmail != "lifecycle@example.com" || localTask.Session != session || localTask.RemoteTaskID != "rdm_test_1" || localTask.Status != "success" {
		t.Fatalf("stored local task=%#v err=%v", localTask, err)
	}
	taskListReq := httptest.NewRequest(http.MethodGet, "/api/admin/chatgpt-tasks?q=lifecycle@example.com", nil)
	taskListReq.AddCookie(cookie)
	taskListResp := httptest.NewRecorder()
	router.ServeHTTP(taskListResp, taskListReq)
	if taskListResp.Code != http.StatusOK || !strings.Contains(taskListResp.Body.String(), "lifecycle@example.com") || strings.Contains(taskListResp.Body.String(), "accessToken") {
		t.Fatalf("admin task list invalid or leaked session: %d %s", taskListResp.Code, taskListResp.Body.String())
	}
	taskDetailReq := httptest.NewRequest(http.MethodGet, fmt.Sprintf("/api/admin/chatgpt-tasks/%d", localTask.ID), nil)
	taskDetailReq.AddCookie(cookie)
	taskDetailResp := httptest.NewRecorder()
	router.ServeHTTP(taskDetailResp, taskDetailReq)
	if taskDetailResp.Code != http.StatusOK || !strings.Contains(taskDetailResp.Body.String(), "accessToken") {
		t.Fatalf("admin task detail did not include retained session: %d %s", taskDetailResp.Code, taskDetailResp.Body.String())
	}

	exportReq := httptest.NewRequest(http.MethodGet, "/api/admin/chatgpt-cdks/export?sku=pro", nil)
	exportReq.AddCookie(cookie)
	exportResp := httptest.NewRecorder()
	router.ServeHTTP(exportResp, exportReq)
	if exportResp.Code != http.StatusOK || !strings.Contains(exportResp.Body.String(), code) || !strings.Contains(exportResp.Header().Get("Content-Type"), "text/csv") {
		t.Fatalf("export response: %d %s", exportResp.Code, exportResp.Body.String())
	}
}

func TestChatGPTCDKReleasesClaimWhenUpstreamRejects(t *testing.T) {
	session := testChatGPTSession("failure@example.com")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, `{"success":false,"error":{"code":"SERVICE_UNAVAILABLE","message":"try later"}}`)
	}))
	defer upstream.Close()

	env := newIntegrationEnv(t)
	env.server.chatGPTRedeemBaseURL = upstream.URL
	env.server.chatGPTRedeemAPIKey = "secret"
	admin, _ := env.store.AdminByUsername(t.Context(), "root")
	items, err := env.store.CreateChatGPTCDKs(t.Context(), []string{"53ed0ec0-6e3d-45d4-b272-ddea5069a8e2"}, "plus", "CDK-FAILURE", "", admin.ID)
	if err != nil {
		t.Fatal(err)
	}
	const token = "ccm_chatgpt_failure"
	if _, err = env.store.CreateAPIKey(t.Context(), "failure", "ccm_fail", tokenHash(token), admin.ID); err != nil {
		t.Fatal(err)
	}
	resp := requestWithAPIKey(t, env.router, http.MethodPost, "/api/chatgpt/cdk/redeem", token, redeemBody(items[0].Code, session))
	if resp.Code != http.StatusServiceUnavailable {
		t.Fatalf("status=%d body=%s", resp.Code, resp.Body.String())
	}
	stored, err := env.store.ChatGPTCDKByCode(t.Context(), items[0].Code)
	if err != nil || stored.Status != "available" || stored.Used {
		t.Fatalf("claim not released: %#v err=%v", stored, err)
	}
	var localTaskID int64
	if err = env.store.DB.QueryRowContext(t.Context(), `SELECT id FROM chatgpt_tasks WHERE cdk_id=? ORDER BY id DESC LIMIT 1`, items[0].ID).Scan(&localTaskID); err != nil {
		t.Fatal(err)
	}
	localTask, err := env.store.ChatGPTTaskByID(t.Context(), localTaskID)
	if err != nil || localTask.Status != "create_failed" || localTask.UserEmail != "failure@example.com" || localTask.Session != session || localTask.ErrorCode != "SERVICE_UNAVAILABLE" {
		t.Fatalf("failed task was not retained: %#v err=%v", localTask, err)
	}
}

func TestChatGPTCDKOrderAllocationDownloadAndCancel(t *testing.T) {
	env := newIntegrationEnv(t)
	admin, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	codes := []string{"9f079b92-47a7-4bbf-8bd2-019d11d39001", "9f079b92-47a7-4bbf-8bd2-019d11d39002"}
	if _, err = env.store.CreateChatGPTCDKs(t.Context(), codes, "prolite", "CDK-ORDER-BATCH", "test order", admin.ID); err != nil {
		t.Fatal(err)
	}

	first := adminRequest(t, env, http.MethodPost, "/api/admin/orders", `{"buyer":"cdk-buyer","quantity":1,"productType":"chatgpt_cdk","cdkSku":"prolite","salePriceCents":1999}`)
	requireStatus(t, first, http.StatusOK)
	firstData := responseData(t, first)
	firstID := numericID(t, firstData, "id")
	if firstData["productType"] != "chatgpt_cdk" || firstData["cdkSku"] != "prolite" {
		t.Fatalf("unexpected cdk order: %s", first.Body.String())
	}
	detail := adminRequest(t, env, http.MethodGet, fmt.Sprintf("/api/admin/orders/%d", firstID), "")
	requireStatus(t, detail, http.StatusOK)
	if !strings.Contains(detail.Body.String(), codes[0]) || !strings.Contains(detail.Body.String(), `"accounts":[]`) {
		t.Fatalf("unexpected order detail: %s", detail.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodPost, fmt.Sprintf("/api/admin/orders/%d/cancel", firstID), `{}`), http.StatusOK)
	released, err := env.store.ChatGPTCDKByCode(t.Context(), codes[0])
	if err != nil || released.OrderNo != "CDK-ORDER-BATCH" {
		t.Fatalf("cancel changed cdk generation order: %#v err=%v", released, err)
	}

	second := adminRequest(t, env, http.MethodPost, "/api/admin/orders", `{"buyer":"cdk-buyer-2","quantity":2,"productType":"chatgpt_cdk","cdkSku":"prolite"}`)
	requireStatus(t, second, http.StatusOK)
	secondID := numericID(t, responseData(t, second), "id")
	download := adminRequest(t, env, http.MethodGet, fmt.Sprintf("/api/admin/orders/%d/download", secondID), "")
	requireStatus(t, download, http.StatusOK)
	if !strings.Contains(download.Body.String(), codes[0]) || !strings.Contains(download.Body.String(), codes[1]) || strings.Contains(download.Body.String(), "----") {
		t.Fatalf("unexpected cdk download: %s", download.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodPost, fmt.Sprintf("/api/admin/orders/%d/cancel", secondID), `{}`), http.StatusBadRequest)
}

func TestPublicRedeemPageAPIsWithoutAPIKey(t *testing.T) {
	session := testChatGPTSession("public@example.com")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodPost {
			var body struct {
				Session string `json:"session"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if body.Session != session {
				t.Fatalf("session was not forwarded as a string: %#v", body)
			}
			fmt.Fprint(w, `{"success":true,"data":{"taskId":"rdm_public_1","status":"pending"}}`)
			return
		}
		fmt.Fprint(w, `{"success":true,"data":{"taskId":"rdm_public_1","status":"success","result":{"message":"Account upgraded successfully"}}}`)
	}))
	defer upstream.Close()

	env := newIntegrationEnv(t)
	env.server.chatGPTRedeemBaseURL = upstream.URL
	env.server.chatGPTRedeemAPIKey = "upstream-key"
	admin, _ := env.store.AdminByUsername(t.Context(), "root")
	const code = "68f337ff-cf34-4527-9c5a-d4b13e7785a8"
	if _, err := env.store.CreateChatGPTCDKs(t.Context(), []string{code}, "plus", "CDK-PUBLIC", "", admin.ID); err != nil {
		t.Fatal(err)
	}

	request := func(path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		resp := httptest.NewRecorder()
		env.router.ServeHTTP(resp, req)
		return resp
	}
	check := request("/api/chatgpt/redeem/check", `{"code":"`+code+`"}`)
	requireStatus(t, check, http.StatusOK)
	if !strings.Contains(check.Body.String(), `"available":true`) {
		t.Fatalf("public check: %s", check.Body.String())
	}
	submit := request("/api/chatgpt/redeem/submit", redeemBody(code, session))
	requireStatus(t, submit, http.StatusOK)
	var submitResult struct {
		Data struct {
			TaskID string `json:"taskId"`
		} `json:"data"`
	}
	if err := json.Unmarshal(submit.Body.Bytes(), &submitResult); err != nil || !strings.HasPrefix(submitResult.Data.TaskID, "ctk_") || strings.Contains(submit.Body.String(), "rdm_public_1") {
		t.Fatalf("public task id was not local hash: %s err=%v", submit.Body.String(), err)
	}
	wrong := request("/api/chatgpt/redeem/task", `{"code":"wrong-code","taskId":"`+submitResult.Data.TaskID+`"}`)
	requireStatus(t, wrong, http.StatusNotFound)
	status := request("/api/chatgpt/redeem/task", `{"code":"`+code+`","taskId":"`+submitResult.Data.TaskID+`"}`)
	requireStatus(t, status, http.StatusOK)
	if !strings.Contains(status.Body.String(), `"status":"success"`) || strings.Contains(status.Body.String(), "rdm_public_1") || !strings.Contains(status.Body.String(), submitResult.Data.TaskID) {
		t.Fatalf("public task status: %s", status.Body.String())
	}
	stored, err := env.store.ChatGPTCDKByCode(t.Context(), code)
	if err != nil || stored.TaskID == nil {
		t.Fatalf("public cdk task link: %#v err=%v", stored, err)
	}
	localTask, err := env.store.ChatGPTTaskByID(t.Context(), *stored.TaskID)
	if err != nil || localTask.UserEmail != "public@example.com" || localTask.Session != session || localTask.Status != "success" || localTask.APIKeyID != nil {
		t.Fatalf("public local task=%#v err=%v", localTask, err)
	}
}

func TestPublicRedeemErrorDoesNotExposeUpstreamDetails(t *testing.T) {
	env := newIntegrationEnv(t)
	env.server.chatGPTRedeemBaseURL = "https://secret-upstream.example"
	env.server.chatGPTRedeemAPIKey = "secret"
	env.server.chatGPTRedeemHTTPClient = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, errors.New(`dial tcp: lookup secret-upstream.example: no such host`)
	})}
	admin, _ := env.store.AdminByUsername(t.Context(), "root")
	const code = "dc6c4510-b4ec-4e78-aa68-24323da19790"
	if _, err := env.store.CreateChatGPTCDKs(t.Context(), []string{code}, "plus", "CDK-REDACT", "", admin.ID); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/chatgpt/redeem/submit", strings.NewReader(redeemBody(code, testChatGPTSession("redact@example.com"))))
	req.Header.Set("Content-Type", "application/json")
	resp := httptest.NewRecorder()
	env.router.ServeHTTP(resp, req)
	requireStatus(t, resp, http.StatusBadGateway)
	if !strings.Contains(resp.Body.String(), "升级服务暂时不可用") || strings.Contains(resp.Body.String(), "secret-upstream") || strings.Contains(resp.Body.String(), "lookup") {
		t.Fatalf("public error was not redacted: %s", resp.Body.String())
	}
	var taskID int64
	if err := env.store.DB.QueryRowContext(t.Context(), `SELECT id FROM chatgpt_tasks WHERE cdk_code=?`, code).Scan(&taskID); err != nil {
		t.Fatal(err)
	}
	task, err := env.store.ChatGPTTaskByID(t.Context(), taskID)
	if err != nil || !strings.Contains(task.ErrorMessage, "secret-upstream.example") {
		t.Fatalf("admin diagnostics were not retained: %#v err=%v", task, err)
	}
}
