package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ccmax/conf"
	"ccmax/dao"

	"github.com/gin-gonic/gin"
)

func TestMultipleAdminsAndRoleIsolation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store, err := dao.Open(filepath.Join(t.TempDir(), "api.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	server := New(store, conf.Config{BootstrapUser: "root", BootstrapPass: "RootPass123", SessionTTL: time.Hour})
	if err = server.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	server.Setup(router)

	rootCookie := loginForTest(t, router, "root", "RootPass123")
	createReq := httptest.NewRequest(http.MethodPost, "/api/admin/admin-users", strings.NewReader(`{"username":"operator","password":"Operator123","displayName":"Operator","role":"admin"}`))
	createReq.Header.Set("Content-Type", "application/json")
	createReq.AddCookie(rootCookie)
	createResp := httptest.NewRecorder()
	router.ServeHTTP(createResp, createReq)
	if createResp.Code != http.StatusOK {
		t.Fatalf("create second admin: status=%d body=%s", createResp.Code, createResp.Body.String())
	}

	operatorCookie := loginForTest(t, router, "operator", "Operator123")
	forbiddenReq := httptest.NewRequest(http.MethodGet, "/api/admin/admin-users", nil)
	forbiddenReq.AddCookie(operatorCookie)
	forbiddenResp := httptest.NewRecorder()
	router.ServeHTTP(forbiddenResp, forbiddenReq)
	if forbiddenResp.Code != http.StatusForbidden {
		t.Fatalf("regular admin accessed admin management: status=%d", forbiddenResp.Code)
	}

	accountsReq := httptest.NewRequest(http.MethodGet, "/api/admin/claude-accounts", nil)
	accountsReq.AddCookie(operatorCookie)
	accountsResp := httptest.NewRecorder()
	router.ServeHTTP(accountsResp, accountsReq)
	if accountsResp.Code != http.StatusOK {
		t.Fatalf("regular admin could not access business page: status=%d body=%s", accountsResp.Code, accountsResp.Body.String())
	}
}

func TestBootstrapRejectsMissingPassword(t *testing.T) {
	store, err := dao.Open(filepath.Join(t.TempDir(), "bootstrap.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err = New(store, conf.Config{BootstrapUser: "root"}).Bootstrap(); err == nil {
		t.Fatal("expected missing bootstrap password to fail")
	}
}

func TestAPIAddsSingleAndBatchFreeAccounts(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store, err := dao.Open(filepath.Join(t.TempDir(), "add-accounts.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	server := New(store, conf.Config{BootstrapUser: "root", BootstrapPass: "RootPass123", SessionTTL: time.Hour, MaxDispatchCount: 100})
	if err = server.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	admin, err := store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const apiToken = "ccm_add_accounts_test"
	if _, err = store.CreateAPIKey(t.Context(), "add-test", "ccm_add", tokenHash(apiToken), admin.ID); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	server.Setup(router)

	body := `{"accounts":[{"mail":"one@example.com","password":"p1","sessionKey":"sk1"},{"mail":"two@example.com","password":"p2","sessionKey":"sk2"}]}`
	resp := requestWithAPIKey(t, router, http.MethodPost, "/api/claude_account/add", apiToken, body)
	if resp.Code != http.StatusOK {
		t.Fatalf("batch add: status=%d body=%s", resp.Code, resp.Body.String())
	}
	var result struct {
		Data struct{ Created, Duplicates int } `json:"data"`
	}
	if err = json.Unmarshal(resp.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Data.Created != 2 || result.Data.Duplicates != 0 {
		t.Fatalf("unexpected batch result: %s", resp.Body.String())
	}

	duplicate := requestWithAPIKey(t, router, http.MethodPost, "/api/claude_account/add", apiToken, body)
	if err = json.Unmarshal(duplicate.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Data.Created != 0 || result.Data.Duplicates != 2 {
		t.Fatalf("unexpected duplicate result: %s", duplicate.Body.String())
	}

	single := requestWithAPIKey(t, router, http.MethodPost, "/api/claude_account/add", apiToken, `{"mail":"single@example.com","password":"p3","sessionKey":"sk3"}`)
	if single.Code != http.StatusOK || !strings.Contains(single.Body.String(), `"created":1`) {
		t.Fatalf("single add: status=%d body=%s", single.Code, single.Body.String())
	}
	accounts, total, err := store.ListAccounts(t.Context(), 1, 20, "", "", 0)
	if err != nil || total != 3 {
		t.Fatalf("stored accounts: total=%d len=%d err=%v", total, len(accounts), err)
	}
	for _, account := range accounts {
		if account.Plan != "free" || account.Status != 1 {
			t.Fatalf("account was not forced to active free plan: %#v", account)
		}
	}
}

func TestAPIDispatchLeaseAndRelease(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store, err := dao.Open(filepath.Join(t.TempDir(), "delivery-report.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	server := New(store, conf.Config{BootstrapUser: "root", BootstrapPass: "RootPass123", SessionTTL: time.Hour, DispatchLease: 30 * time.Minute, MaxDispatchCount: 100})
	if err = server.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	admin, err := store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const apiToken = "ccm_delivery_test"
	if _, err = store.CreateAPIKey(t.Context(), "delivery-test", "ccm_delivery", tokenHash(apiToken), admin.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = store.CreateAccount(t.Context(), dao.ClaudeAccount{Mail: "lease@example.com", Password: "pass", SessionKey: "session", Plan: "free"}); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	server.Setup(router)

	req := httptest.NewRequest(http.MethodPost, "/api/claude_account", strings.NewReader(`{"count":1}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", apiToken)
	req.Header.Set("Idempotency-Key", "api-report-request")
	dispatch := httptest.NewRecorder()
	router.ServeHTTP(dispatch, req)
	if dispatch.Code != http.StatusOK || !strings.Contains(dispatch.Body.String(), `"leaseExpiresAt"`) {
		t.Fatalf("dispatch lease response: status=%d body=%s", dispatch.Code, dispatch.Body.String())
	}

	release := requestWithAPIKey(t, router, http.MethodPost, "/api/claude_account/release", apiToken, `{"requestId":"api-report-request","mails":["lease@example.com"]}`)
	if release.Code != http.StatusOK || !strings.Contains(release.Body.String(), `"released":1`) {
		t.Fatalf("failed-account release: status=%d body=%s", release.Code, release.Body.String())
	}
	accounts, _, err := store.ListAccounts(t.Context(), 1, 10, "lease@example.com", "", 0)
	if err != nil || len(accounts) != 1 || accounts[0].DeliveryStatus != "available" {
		t.Fatalf("account release state not persisted: %#v %v", accounts, err)
	}
}

func TestAPIDispatchesCardAndProtectsVerificationByAPIKey(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store, err := dao.Open(filepath.Join(t.TempDir(), "card-dispatch.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	server := New(store, conf.Config{BootstrapUser: "root", BootstrapPass: "RootPass123", SessionTTL: time.Hour, MaxDispatchCount: 100})
	if err = server.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	admin, err := store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const ownerToken = "ccm_card_owner"
	const otherToken = "ccm_card_other"
	if _, err = store.CreateAPIKey(t.Context(), "card-owner", "ccm_owner", tokenHash(ownerToken), admin.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = store.CreateAPIKey(t.Context(), "card-other", "ccm_other", tokenHash(otherToken), admin.ID); err != nil {
		t.Fatal(err)
	}
	cardPoolID, err := store.CreateCard(t.Context(), dao.Card{Source: "qbit", CardID: "channel-card-1", CardNo: "4111111111111111", ExpireMMYY: "1228", CCV: "123"})
	if err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	server.Setup(router)

	secondCardPoolID, err := store.CreateCard(t.Context(), dao.Card{Source: "qbit", CardID: "channel-card-2", CardNo: "4111111111111112", ExpireMMYY: "1228", CCV: "124"})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/card", strings.NewReader(`{"count":2,"source":"qbit"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", ownerToken)
	req.Header.Set("Idempotency-Key", "card-api-request")
	resp := httptest.NewRecorder()
	router.ServeHTTP(resp, req)
	if resp.Code != http.StatusOK || !strings.Contains(resp.Body.String(), `"cardPoolId":`+fmt.Sprint(cardPoolID)) || !strings.Contains(resp.Body.String(), `"cardNo":"4111111111111111"`) {
		t.Fatalf("card dispatch: status=%d body=%s", resp.Code, resp.Body.String())
	}

	forbidden := requestWithAPIKey(t, router, http.MethodPost, "/api/card/verify-code", otherToken, fmt.Sprintf(`{"cardPoolId":%d,"googleRef":"BMR"}`, cardPoolID))
	if forbidden.Code != http.StatusNotFound {
		t.Fatalf("other API key should not verify card: status=%d body=%s", forbidden.Code, forbidden.Body.String())
	}
	usedReportBody := fmt.Sprintf(`{"requestId":"card-api-request","cards":[{"cardPoolId":%d,"status":"used"}]}`, cardPoolID)
	usedReport := requestWithAPIKey(t, router, http.MethodPost, "/api/card/report", ownerToken, usedReportBody)
	if usedReport.Code != http.StatusOK || !strings.Contains(usedReport.Body.String(), `"reported":1`) {
		t.Fatalf("card used report: status=%d body=%s", usedReport.Code, usedReport.Body.String())
	}
	if retry := requestWithAPIKey(t, router, http.MethodPost, "/api/card/report", ownerToken, usedReportBody); retry.Code != http.StatusOK {
		t.Fatalf("card used report retry: status=%d body=%s", retry.Code, retry.Body.String())
	}
	cooled, err := store.CardByID(t.Context(), cardPoolID)
	if err != nil || cooled.UsageCount != 1 || cooled.CooldownUntil == nil || cooled.CooldownUntil.Before(time.Now().Add(4*time.Hour+59*time.Minute)) {
		t.Fatalf("used card did not enter cooldown exactly once: %#v %v", cooled, err)
	}
	report := requestWithAPIKey(t, router, http.MethodPost, "/api/card/report", ownerToken, fmt.Sprintf(`{"requestId":"card-api-request","cards":[{"cardPoolId":%d,"status":"unavailable","reason":"declined"}]}`, secondCardPoolID))
	if report.Code != http.StatusOK || !strings.Contains(report.Body.String(), `"reported":1`) {
		t.Fatalf("card unavailable report: status=%d body=%s", report.Code, report.Body.String())
	}
	ownerVerify := requestWithAPIKey(t, router, http.MethodPost, "/api/card/verify-code", ownerToken, fmt.Sprintf(`{"cardPoolId":%d,"googleRef":"BMR"}`, secondCardPoolID))
	if ownerVerify.Code != http.StatusNotFound {
		t.Fatalf("unavailable card should not be verified: status=%d body=%s", ownerVerify.Code, ownerVerify.Body.String())
	}
}

func requestWithAPIKey(t *testing.T, router http.Handler, method, path, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", token)
	resp := httptest.NewRecorder()
	router.ServeHTTP(resp, req)
	return resp
}

func loginForTest(t *testing.T, router http.Handler, username, password string) *http.Cookie {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/admin/auth/login", strings.NewReader(`{"username":"`+username+`","password":"`+password+`"}`))
	req.Header.Set("Content-Type", "application/json")
	resp := httptest.NewRecorder()
	router.ServeHTTP(resp, req)
	if resp.Code != http.StatusOK {
		t.Fatalf("login %s: status=%d body=%s", username, resp.Code, resp.Body.String())
	}
	for _, cookie := range resp.Result().Cookies() {
		if cookie.Name == sessionCookie {
			return cookie
		}
	}
	t.Fatalf("login %s returned no session cookie", username)
	return nil
}

func TestAdminCanDisableEnableAndDeleteAPIKey(t *testing.T) {
	env := newIntegrationEnv(t)
	created := adminRequest(t, env, http.MethodPost, "/api/admin/api-keys", `{"name":"removable-key"}`)
	requireStatus(t, created, http.StatusOK)
	data := responseData(t, created)
	id := numericID(t, data, "id")
	token, _ := data["key"].(string)
	checkPath := "/api/chatgpt/cdk/check"
	body := `{"code":"missing-code"}`

	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, checkPath, token, body), http.StatusNotFound)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/api-keys/%d/status", id), `{"status":-1}`), http.StatusOK)
	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, checkPath, token, body), http.StatusUnauthorized)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/api-keys/%d/status", id), `{"status":1}`), http.StatusOK)
	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, checkPath, token, body), http.StatusNotFound)
	requireStatus(t, adminRequest(t, env, http.MethodDelete, fmt.Sprintf("/api/admin/api-keys/%d", id), ""), http.StatusOK)
	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, checkPath, token, body), http.StatusUnauthorized)
	listed := adminRequest(t, env, http.MethodGet, "/api/admin/api-keys", "")
	requireStatus(t, listed, http.StatusOK)
	if strings.Contains(listed.Body.String(), "removable-key") {
		t.Fatalf("deleted key remained visible: %s", listed.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/api-keys/%d/status", id), `{"status":1}`), http.StatusNotFound)
}
