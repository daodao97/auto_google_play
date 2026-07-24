package api

import (
	"encoding/json"
	"fmt"
	"io"
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

type integrationEnv struct {
	store  *dao.Store
	server *Server
	router http.Handler
	cookie *http.Cookie
}

func newIntegrationEnv(t *testing.T) *integrationEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)
	store, err := dao.Open(filepath.Join(t.TempDir(), "integration.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	cfg := conf.Config{BootstrapUser: "root", BootstrapPass: "RootPass123", SessionTTL: time.Hour, DispatchLease: 30 * time.Minute, MaxDispatchCount: 100}
	server := New(store, cfg)
	if err = server.Bootstrap(); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	server.Setup(router)
	return &integrationEnv{store: store, server: server, router: router, cookie: loginForTest(t, router, "root", "RootPass123")}
}

func adminRequest(t *testing.T, env *integrationEnv, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	req.AddCookie(env.cookie)
	resp := httptest.NewRecorder()
	env.router.ServeHTTP(resp, req)
	return resp
}

func requireStatus(t *testing.T, resp *httptest.ResponseRecorder, want int) {
	t.Helper()
	if resp.Code != want {
		t.Fatalf("status=%d want=%d body=%s", resp.Code, want, resp.Body.String())
	}
}

func responseData(t *testing.T, resp *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var payload struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(resp.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode response %q: %v", resp.Body.String(), err)
	}
	return payload.Data
}

func numericID(t *testing.T, data map[string]any, key string) int64 {
	t.Helper()
	value, ok := data[key].(float64)
	if !ok || value <= 0 {
		t.Fatalf("missing positive %s in %#v", key, data)
	}
	return int64(value)
}

func seedCoolingAndDisabledCards(t *testing.T, store *dao.Store, source string) {
	t.Helper()
	coolingID, err := store.CreateCard(t.Context(), dao.Card{
		Source: source, CardID: "cooling-card", CardNo: "4111111111110101", ExpireMMYY: "1230", CCV: "101",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = store.DB.ExecContext(t.Context(), `UPDATE card_pool SET cooldown_until=? WHERE id=?`, time.Now().Add(time.Hour), coolingID); err != nil {
		t.Fatal(err)
	}
	if _, err = store.CreateCard(t.Context(), dao.Card{
		Source: source, CardID: "disabled-card", CardNo: "4111111111110202", ExpireMMYY: "1230", CCV: "202", Status: -1,
	}); err != nil {
		t.Fatal(err)
	}
}

func requireErrorCode(t *testing.T, resp *httptest.ResponseRecorder, status int, code string) {
	t.Helper()
	requireStatus(t, resp, status)
	var payload struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal(resp.Body.Bytes(), &payload); err != nil || payload.Code != code {
		t.Fatalf("error contract status=%d code=%q want=%q body=%s err=%v", resp.Code, payload.Code, code, resp.Body.String(), err)
	}
}

func TestAdminAndBusinessAPIsEndToEnd(t *testing.T) {
	env := newIntegrationEnv(t)

	health := httptest.NewRecorder()
	env.router.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/api/health", nil))
	requireStatus(t, health, http.StatusOK)
	unauthorized := httptest.NewRecorder()
	env.router.ServeHTTP(unauthorized, httptest.NewRequest(http.MethodGet, "/api/admin/dashboard", nil))
	requireStatus(t, unauthorized, http.StatusUnauthorized)
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/auth/me", ""), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/dashboard", ""), http.StatusOK)

	account := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"admin-free@example.com","password":"p1","sessionKey":"sk-admin-free","plan":"free","status":1}`)
	requireStatus(t, account, http.StatusOK)
	accountID := numericID(t, responseData(t, account), "id")
	imported := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts/import", `{"accounts":[{"mail":"order-one@example.com","password":"p2","sessionKey":"sk-order-1","plan":"max_20x","status":1},{"mail":"order-two@example.com","password":"p3","sessionKey":"sk-order-2","plan":"max_20x","status":1}]}`)
	requireStatus(t, imported, http.StatusOK)
	if !strings.Contains(imported.Body.String(), `"created":2`) {
		t.Fatalf("account import result: %s", imported.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/claude-accounts?q=admin-free&plan=free&status=1", ""), http.StatusOK)
	updatedAccount := `{"mail":"admin-free@example.com","password":"p1-new","sessionKey":"sk-admin-free-new","plan":"free","status":1}`
	requireStatus(t, adminRequest(t, env, http.MethodPut, fmt.Sprintf("/api/admin/claude-accounts/%d", accountID), updatedAccount), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/claude-accounts/%d/status", accountID), `{"status":-1}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/claude-accounts/%d/status", accountID), `{"status":1}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, "/api/admin/claude-accounts/not-a-number/status", `{"status":1}`), http.StatusBadRequest)

	card := adminRequest(t, env, http.MethodPost, "/api/admin/cards", `{"source":"qbit","cardId":"channel-admin-1","cardNo":"4111111111111111","expireMmyy":"1228","ccv":"123","status":1}`)
	requireStatus(t, card, http.StatusOK)
	cardID := numericID(t, responseData(t, card), "id")
	cardImport := adminRequest(t, env, http.MethodPost, "/api/admin/cards/import", `{"source":"qbit","lines":"4222222222222222 1128 456 channel-admin-2\n6264259812798577  04/29  414  23234234\n5177467478887927  Ku Kan  07/29  217 asdfasdf\ninvalid"}`)
	requireStatus(t, cardImport, http.StatusOK)
	if !strings.Contains(cardImport.Body.String(), `"created":3`) || !strings.Contains(cardImport.Body.String(), `格式错误`) {
		t.Fatalf("card import result: %s", cardImport.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/cards?source=qbit&status=1", ""), http.StatusOK)
	cardUpdate := `{"source":"qbit","cardId":"channel-admin-1","cardNo":"4111111111111111","expireMmyy":"0129","ccv":"321","status":1}`
	requireStatus(t, adminRequest(t, env, http.MethodPut, fmt.Sprintf("/api/admin/cards/%d", cardID), cardUpdate), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/cards/%d/status", cardID), `{"status":-1}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/cards/%d/status", cardID), `{"status":1}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, "/api/admin/cards/bad/status", `{"status":1}`), http.StatusBadRequest)
	requireStatus(t, adminRequest(t, env, http.MethodPut, "/api/admin/channel-credentials/qbit", `{"token":"integration-qbit-token"}`), http.StatusOK)
	credentials := adminRequest(t, env, http.MethodGet, "/api/admin/channel-credentials", "")
	requireStatus(t, credentials, http.StatusOK)
	if strings.Contains(credentials.Body.String(), "integration-qbit-token") || !strings.Contains(credentials.Body.String(), `"tokenConfigured":true`) {
		t.Fatalf("credential list leaked token or missed configured state: %s", credentials.Body.String())
	}

	keyResp := adminRequest(t, env, http.MethodPost, "/api/admin/api-keys", `{"name":"integration"}`)
	requireStatus(t, keyResp, http.StatusOK)
	keyData := responseData(t, keyResp)
	apiKeyID := numericID(t, keyData, "id")
	apiToken, _ := keyData["key"].(string)
	if !strings.HasPrefix(apiToken, "ccm_") {
		t.Fatalf("unexpected API key response: %#v", keyData)
	}
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/api-keys", ""), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, "/api/admin/api-keys/bad/status", `{"status":1}`), http.StatusBadRequest)

	add := requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/add", apiToken, `{"accounts":[{"mail":"public-one@example.com","password":"pw1","sessionKey":"pub-sk-1"},{"mail":"public-two@example.com","password":"pw2","sessionKey":"pub-sk-2"}]}`)
	requireStatus(t, add, http.StatusOK)
	dispatchReq := httptest.NewRequest(http.MethodPost, "/api/claude_account", strings.NewReader(`{"count":1,"plan":"free"}`))
	dispatchReq.Header.Set("Content-Type", "application/json")
	dispatchReq.Header.Set("Authorization", "Bearer "+apiToken)
	dispatchReq.Header.Set("Idempotency-Key", "integration-account-dispatch")
	dispatch := httptest.NewRecorder()
	env.router.ServeHTTP(dispatch, dispatchReq)
	requireStatus(t, dispatch, http.StatusOK)
	dispatchData := responseData(t, dispatch)
	accounts, _ := dispatchData["accounts"].([]any)
	if len(accounts) != 1 || dispatchData["leaseExpiresAt"] == nil {
		t.Fatalf("dispatch response missing account or lease: %#v", dispatchData)
	}
	dispatchedMail := accounts[0].(map[string]any)["mail"].(string)
	retryReq := httptest.NewRequest(http.MethodPost, "/api/claude_account", strings.NewReader(`{"count":1}`))
	retryReq.Header.Set("Content-Type", "application/json")
	retryReq.Header.Set("X-API-Key", apiToken)
	retryReq.Header.Set("Idempotency-Key", "integration-account-dispatch")
	retry := httptest.NewRecorder()
	env.router.ServeHTTP(retry, retryReq)
	requireStatus(t, retry, http.StatusOK)
	if !strings.Contains(retry.Body.String(), dispatchedMail) {
		t.Fatalf("idempotent dispatch changed account: %s", retry.Body.String())
	}
	release := requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/release", apiToken, fmt.Sprintf(`{"requestId":"integration-account-dispatch","mails":[%q]}`, dispatchedMail))
	requireStatus(t, release, http.StatusOK)

	adminReleaseReq := httptest.NewRequest(http.MethodPost, "/api/claude_account", strings.NewReader(`{"count":1}`))
	adminReleaseReq.Header.Set("Content-Type", "application/json")
	adminReleaseReq.Header.Set("X-API-Key", apiToken)
	adminReleaseReq.Header.Set("Idempotency-Key", "integration-admin-release")
	adminReleaseDispatch := httptest.NewRecorder()
	env.router.ServeHTTP(adminReleaseDispatch, adminReleaseReq)
	requireStatus(t, adminReleaseDispatch, http.StatusOK)
	adminReleaseData := responseData(t, adminReleaseDispatch)
	adminReleaseMail := adminReleaseData["accounts"].([]any)[0].(map[string]any)["mail"].(string)
	accountRows, _, err := env.store.ListAccounts(t.Context(), 1, 10, adminReleaseMail, "", 0)
	if err != nil || len(accountRows) != 1 {
		t.Fatalf("find locked account for admin release: %#v %v", accountRows, err)
	}
	requireStatus(t, adminRequest(t, env, http.MethodPost, fmt.Sprintf("/api/admin/claude-accounts/%d/release", accountRows[0].ID), `{}`), http.StatusOK)

	upgrade := requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/upgrade", apiToken, fmt.Sprintf(`{"mail":%q,"plan":"max_20x","cardPoolId":%d}`, adminReleaseMail, cardID))
	requireStatus(t, upgrade, http.StatusOK)
	if !strings.Contains(upgrade.Body.String(), `"plan":"max_20x"`) || !strings.Contains(upgrade.Body.String(), fmt.Sprintf(`"cardPoolId":%d`, cardID)) || !strings.Contains(upgrade.Body.String(), `"deliveryStatus":"upgraded"`) {
		t.Fatalf("upgrade result: %s", upgrade.Body.String())
	}
	cooledCard, err := env.store.CardByID(t.Context(), cardID)
	if err != nil || cooledCard.UsageCount != 1 || cooledCard.CooldownUntil == nil || cooledCard.CooldownUntil.Before(time.Now().Add(4*time.Hour+59*time.Minute)) {
		t.Fatalf("upgrade API did not cool card for five hours: %#v %v", cooledCard, err)
	}
	cardList := adminRequest(t, env, http.MethodGet, "/api/admin/cards", "")
	requireStatus(t, cardList, http.StatusOK)
	if !strings.Contains(cardList.Body.String(), `"stats":{"available":3,"cooling":1,"total":4}`) {
		t.Fatalf("card list response missing inventory stats: %s", cardList.Body.String())
	}

	cardDispatchReq := httptest.NewRequest(http.MethodPost, "/api/card", strings.NewReader(`{"count":1,"source":"qbit"}`))
	cardDispatchReq.Header.Set("Content-Type", "application/json")
	cardDispatchReq.Header.Set("X-API-Key", apiToken)
	cardDispatchReq.Header.Set("Idempotency-Key", "integration-card-dispatch")
	cardDispatch := httptest.NewRecorder()
	env.router.ServeHTTP(cardDispatch, cardDispatchReq)
	requireStatus(t, cardDispatch, http.StatusOK)
	cardDispatchData := responseData(t, cardDispatch)
	cards := cardDispatchData["cards"].([]any)
	dispatchedCardID := int64(cards[0].(map[string]any)["cardPoolId"].(float64))
	if dispatchedCardID == cardID {
		t.Fatalf("upgrade-cooled card was dispatched again: %d", dispatchedCardID)
	}
	cardReport := requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/report", apiToken, fmt.Sprintf(`{"requestId":"integration-card-dispatch","cards":[{"cardPoolId":%d,"status":"unavailable","reason":"integration"}]}`, dispatchedCardID))
	requireStatus(t, cardReport, http.StatusOK)
	if !strings.Contains(cardReport.Body.String(), `"reported":1`) {
		t.Fatalf("card report result: %s", cardReport.Body.String())
	}

	orderOne := adminRequest(t, env, http.MethodPost, "/api/admin/orders", `{"buyer":"buyer-a","salePriceCents":1999,"quantity":1,"plan":"max_20x","remark":"download"}`)
	requireStatus(t, orderOne, http.StatusOK)
	batchNo, _ := responseData(t, orderOne)["batchNo"].(string)
	if !strings.HasPrefix(batchNo, "ORD-") {
		t.Fatalf("automatic batch number missing: %s", orderOne.Body.String())
	}
	orderOneID := numericID(t, responseData(t, orderOne), "id")
	requireStatus(t, adminRequest(t, env, http.MethodGet, fmt.Sprintf("/api/admin/orders/%d", orderOneID), ""), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/orders?q=integration&status=allocated", ""), http.StatusOK)
	download := adminRequest(t, env, http.MethodGet, fmt.Sprintf("/api/admin/orders/%d/download", orderOneID), "")
	requireStatus(t, download, http.StatusOK)
	if !strings.Contains(download.Header().Get("Content-Disposition"), batchNo+".txt") || !strings.Contains(download.Body.String(), "----") {
		t.Fatalf("order download headers/body invalid: headers=%v body=%s", download.Header(), download.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodPost, fmt.Sprintf("/api/admin/orders/%d/cancel", orderOneID), `{}`), http.StatusBadRequest)
	orderTwo := adminRequest(t, env, http.MethodPost, "/api/admin/orders", `{"batchNo":"integration-cancel","buyer":"buyer-b","salePriceCents":999,"quantity":1,"plan":"max_20x"}`)
	requireStatus(t, orderTwo, http.StatusOK)
	orderTwoID := numericID(t, responseData(t, orderTwo), "id")
	requireStatus(t, adminRequest(t, env, http.MethodPost, fmt.Sprintf("/api/admin/orders/%d/cancel", orderTwoID), `{}`), http.StatusOK)

	admins := adminRequest(t, env, http.MethodGet, "/api/admin/admin-users", "")
	requireStatus(t, admins, http.StatusOK)
	operator := adminRequest(t, env, http.MethodPost, "/api/admin/admin-users", `{"username":"integration-operator","password":"Operator123","displayName":"Operator","role":"admin"}`)
	requireStatus(t, operator, http.StatusOK)
	operatorID := numericID(t, responseData(t, operator), "id")
	requireStatus(t, adminRequest(t, env, http.MethodPut, fmt.Sprintf("/api/admin/admin-users/%d", operatorID), `{"displayName":"Updated Operator","role":"admin","status":1,"password":"Operator456"}`), http.StatusOK)
	root, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	requireStatus(t, adminRequest(t, env, http.MethodPut, fmt.Sprintf("/api/admin/admin-users/%d", root.ID), `{"displayName":"Root","role":"super_admin","status":-1}`), http.StatusBadRequest)

	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/api-keys/%d/status", apiKeyID), `{"status":-1}`), http.StatusOK)
	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account", apiToken, `{"count":1}`), http.StatusUnauthorized)
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/api-keys/%d/status", apiKeyID), `{"status":1}`), http.StatusOK)

	requireStatus(t, adminRequest(t, env, http.MethodPost, "/api/admin/auth/change-password", `{"currentPassword":"wrong","newPassword":"NewRootPass123"}`), http.StatusBadRequest)
	requireStatus(t, adminRequest(t, env, http.MethodPost, "/api/admin/auth/change-password", `{"currentPassword":"RootPass123","newPassword":"NewRootPass123"}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodPost, "/api/admin/auth/logout", `{}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodGet, "/api/admin/auth/me", ""), http.StatusUnauthorized)
	_ = loginForTest(t, env.router, "root", "NewRootPass123")
}

func TestAdminDeletesUnsoldAccount(t *testing.T) {
	env := newIntegrationEnv(t)
	created := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"delete-me@example.com","password":"pw","sessionKey":"delete-sk","plan":"free","status":1}`)
	requireStatus(t, created, http.StatusOK)
	id := numericID(t, responseData(t, created), "id")
	requireStatus(t, adminRequest(t, env, http.MethodPost, fmt.Sprintf("/api/admin/claude-accounts/%d/reset", id), ""), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodDelete, fmt.Sprintf("/api/admin/claude-accounts/%d", id), ""), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodDelete, fmt.Sprintf("/api/admin/claude-accounts/%d", id), ""), http.StatusNotFound)
}

func TestGoogleAccountPoolDispatchAndUsedReport(t *testing.T) {
	env := newIntegrationEnv(t)
	admin, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const apiToken = "ccm_google_account_integration"
	keyID, err := env.store.CreateAPIKey(t.Context(), "google-account-test", "ccm_google", tokenHash(apiToken), admin.ID)
	if err != nil {
		t.Fatal(err)
	}
	if keyID <= 0 {
		t.Fatal("API key was not created")
	}
	imported := adminRequest(t, env, http.MethodPost, "/api/admin/google-accounts/import", `{"lines":"first-google@example.com|FirstPass123!\nsecond-google@example.com|SecondPass123!\ninvalid"}`)
	requireStatus(t, imported, http.StatusOK)
	if !strings.Contains(imported.Body.String(), `"created":2`) || !strings.Contains(imported.Body.String(), `第 3 行`) {
		t.Fatalf("unexpected Google account import response: %s", imported.Body.String())
	}

	first := requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account", apiToken, `{"requestId":"google-request-1"}`)
	requireStatus(t, first, http.StatusOK)
	firstData := responseData(t, first)
	firstAccount, ok := firstData["account"].(map[string]any)
	if !ok {
		t.Fatalf("missing dispatched Google account: %#v", firstData)
	}
	firstID := numericID(t, firstAccount, "googleAccountId")
	if firstAccount["mail"] != "first-google@example.com" || firstAccount["password"] != "FirstPass123!" {
		t.Fatalf("unexpected first Google account: %#v", firstAccount)
	}
	retry := requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account", apiToken, `{"requestId":"google-request-1"}`)
	requireStatus(t, retry, http.StatusOK)
	if numericID(t, responseData(t, retry)["account"].(map[string]any), "googleAccountId") != firstID {
		t.Fatalf("idempotent dispatch returned another Google account: %s", retry.Body.String())
	}

	reportBody := fmt.Sprintf(`{"requestId":"google-request-1","googleAccountId":%d,"status":"used"}`, firstID)
	reported := requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account/report", apiToken, reportBody)
	requireStatus(t, reported, http.StatusOK)
	if !strings.Contains(reported.Body.String(), `"status":"used"`) || !strings.Contains(reported.Body.String(), `"reportedAt":`) || strings.Contains(reported.Body.String(), "claudeAccount") {
		t.Fatalf("unexpected Google account report response: %s", reported.Body.String())
	}
	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account/report", apiToken, reportBody), http.StatusOK)

	second := requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account", apiToken, `{"requestId":"google-request-2"}`)
	requireStatus(t, second, http.StatusOK)
	if strings.Contains(second.Body.String(), "first-google@example.com") || !strings.Contains(second.Body.String(), "second-google@example.com") {
		t.Fatalf("used Google account was dispatched again: %s", second.Body.String())
	}
	secondAccount := responseData(t, second)["account"].(map[string]any)
	secondID := numericID(t, secondAccount, "googleAccountId")
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/google-accounts/%d/status", secondID), `{"enabled":-1}`), http.StatusOK)
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account", apiToken, `{"requestId":"google-request-3"}`), http.StatusConflict, "INSUFFICIENT_GOOGLE_ACCOUNTS")

	listed := adminRequest(t, env, http.MethodGet, "/api/admin/google-accounts?status=used", "")
	requireStatus(t, listed, http.StatusOK)
	if !strings.Contains(listed.Body.String(), `"status":"used"`) || !strings.Contains(listed.Body.String(), `"used":1`) || strings.Contains(listed.Body.String(), "claudeAccount") {
		t.Fatalf("Google account report status was not listed: %s", listed.Body.String())
	}
	requireStatus(t, adminRequest(t, env, http.MethodDelete, fmt.Sprintf("/api/admin/google-accounts/%d", secondID), ""), http.StatusOK)
	remaining := adminRequest(t, env, http.MethodGet, "/api/admin/google-accounts", "")
	requireStatus(t, remaining, http.StatusOK)
	if !strings.Contains(remaining.Body.String(), `"total":1`) || strings.Contains(remaining.Body.String(), "second-google@example.com") {
		t.Fatalf("Google account was not deleted: %s", remaining.Body.String())
	}
}

func TestMailAccountPoolDispatchAndUsedReport(t *testing.T) {
	env := newIntegrationEnv(t)
	admin, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const apiToken = "ccm_mail_account_integration"
	_, err = env.store.CreateAPIKey(t.Context(), "mail-account-test", "ccm_mail", tokenHash(apiToken), admin.ID)
	if err != nil {
		t.Fatal(err)
	}
	claude := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"mail-linked-claude@example.com","password":"pw","sessionKey":"mail-linked-session","plan":"free","status":1}`)
	requireStatus(t, claude, http.StatusOK)

	imported := adminRequest(t, env, http.MethodPost, "/api/admin/mail-accounts/import", `{"platform":"mailcom","lines":"first@mail.com----FirstPass123!\nsecond@gmx.com|SecondPass123!|gmx\ninvalid"}`)
	requireStatus(t, imported, http.StatusOK)
	if !strings.Contains(imported.Body.String(), `"created":2`) || !strings.Contains(imported.Body.String(), `第 3 行`) {
		t.Fatalf("unexpected mail account import response: %s", imported.Body.String())
	}

	first := requestWithAPIKey(t, env.router, http.MethodPost, "/api/mail_account", apiToken, `{"requestId":"mail-request-1","platform":"mailcom"}`)
	requireStatus(t, first, http.StatusOK)
	firstAccount := responseData(t, first)["account"].(map[string]any)
	firstID := numericID(t, firstAccount, "mailAccountId")
	if firstAccount["mail"] != "first@mail.com" || firstAccount["password"] != "FirstPass123!" || firstAccount["platform"] != "mailcom" {
		t.Fatalf("unexpected mail account: %#v", firstAccount)
	}
	retry := requestWithAPIKey(t, env.router, http.MethodPost, "/api/mail_account", apiToken, `{"requestId":"mail-request-1","platform":"mailcom"}`)
	requireStatus(t, retry, http.StatusOK)
	if numericID(t, responseData(t, retry)["account"].(map[string]any), "mailAccountId") != firstID {
		t.Fatalf("idempotent dispatch returned another mail account: %s", retry.Body.String())
	}

	reportBody := fmt.Sprintf(`{"requestId":"mail-request-1","mailAccountId":%d,"claudeAccountMail":"mail-linked-claude@example.com"}`, firstID)
	reported := requestWithAPIKey(t, env.router, http.MethodPost, "/api/mail_account/report", apiToken, reportBody)
	requireStatus(t, reported, http.StatusOK)
	if !strings.Contains(reported.Body.String(), `"status":"used"`) {
		t.Fatalf("unexpected mail account report response: %s", reported.Body.String())
	}
	requireStatus(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/mail_account/report", apiToken, reportBody), http.StatusOK)

	missing := requestWithAPIKey(t, env.router, http.MethodPost, "/api/mail_account", apiToken, `{"requestId":"mail-request-2","platform":"mailcom"}`)
	requireErrorCode(t, missing, http.StatusConflict, "INSUFFICIENT_MAIL_ACCOUNTS")
	second := requestWithAPIKey(t, env.router, http.MethodPost, "/api/mail_account", apiToken, `{"requestId":"mail-request-3","platform":"gmx"}`)
	requireStatus(t, second, http.StatusOK)
	if strings.Contains(second.Body.String(), "first@mail.com") || !strings.Contains(second.Body.String(), "second@gmx.com") {
		t.Fatalf("used mail account was dispatched again: %s", second.Body.String())
	}
	secondID := numericID(t, responseData(t, second)["account"].(map[string]any), "mailAccountId")
	requireStatus(t, adminRequest(t, env, http.MethodPatch, fmt.Sprintf("/api/admin/mail-accounts/%d/status", secondID), `{"enabled":-1}`), http.StatusOK)
	requireStatus(t, adminRequest(t, env, http.MethodDelete, fmt.Sprintf("/api/admin/mail-accounts/%d", secondID), ""), http.StatusOK)

	listed := adminRequest(t, env, http.MethodGet, "/api/admin/mail-accounts?status=used&platform=mailcom", "")
	requireStatus(t, listed, http.StatusOK)
	if !strings.Contains(listed.Body.String(), `"claudeAccountMail":"mail-linked-claude@example.com"`) || !strings.Contains(listed.Body.String(), `"used":1`) {
		t.Fatalf("mail account association was not listed: %s", listed.Body.String())
	}
}

func TestAdminRegistrationImportsKYCFreeClaudeAccount(t *testing.T) {
	env := newIntegrationEnv(t)
	const runID = "run_1784500000000000000_a1b2c3d4"
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer register-token" {
			t.Errorf("missing registration bearer token: %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/start":
			var payload map[string]any
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			if payload["mail_provider"] != "mailcom" || !strings.Contains(fmt.Sprint(payload["accounts_text"]), "register@mail.com----MailPass123!") {
				t.Errorf("unexpected registration payload: %#v", payload)
			}
			_, _ = w.Write([]byte(`{"ok":true,"run_id":"` + runID + `","count":1}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/status":
			_, _ = w.Write([]byte(`{"running":false,"run_id":"` + runID + `","summary":{"success":1,"kyc_pass":1},"tasks":[{"email":"register@mail.com","status":"success","stage":"done","kyc_status":"not_required","has_session":true}]}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/runs/"+runID+"/kyc_pass.txt":
			w.Header().Set("Content-Type", "text/plain")
			_, _ = w.Write([]byte("register@mail.com----MailPass123!----sk-ant-sid02-registered\n"))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/runs/"+runID+"/"):
			w.WriteHeader(http.StatusNotFound)
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	env.server.registrationBaseURL = upstream.URL
	env.server.registrationPollInterval = 10 * time.Millisecond
	if err := env.store.SetCredential(t.Context(), registrationCredentialSource, "register-token"); err != nil {
		t.Fatal(err)
	}
	requireStatus(t, adminRequest(t, env, http.MethodPost, "/api/admin/mail-accounts/import", `{"platform":"mailcom","lines":"register@mail.com----MailPass123!"}`), http.StatusOK)
	started := adminRequest(t, env, http.MethodPost, "/api/admin/registration/start", `{"platform":"mailcom","count":1,"concurrency":1,"retryMax":0,"proxyMode":"configured"}`)
	requireStatus(t, started, http.StatusOK)
	deadline := time.Now().Add(2 * time.Second)
	for {
		overview := adminRequest(t, env, http.MethodGet, "/api/admin/registration", "")
		requireStatus(t, overview, http.StatusOK)
		if strings.Contains(overview.Body.String(), `"status":"completed"`) {
			if !strings.Contains(overview.Body.String(), `"importedCount":1`) {
				t.Fatalf("unexpected completed registration: %s", overview.Body.String())
			}
			if !strings.Contains(overview.Body.String(), `"claude_account_status":"added"`) {
				t.Fatalf("registration task did not expose Claude account import status: %s", overview.Body.String())
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("registration did not complete: %s", overview.Body.String())
		}
		time.Sleep(10 * time.Millisecond)
	}
	accounts := adminRequest(t, env, http.MethodGet, "/api/admin/claude-accounts?q=register@mail.com", "")
	requireStatus(t, accounts, http.StatusOK)
	if !strings.Contains(accounts.Body.String(), `"sessionKey":"sk-ant-sid02-registered"`) {
		t.Fatalf("registered Claude account was not imported: %s", accounts.Body.String())
	}
	mails := adminRequest(t, env, http.MethodGet, "/api/admin/mail-accounts?q=register@mail.com", "")
	requireStatus(t, mails, http.StatusOK)
	if !strings.Contains(mails.Body.String(), `"status":"used"`) || !strings.Contains(mails.Body.String(), `"claudeAccountMail":"register@mail.com"`) {
		t.Fatalf("registration mail account was not consumed: %s", mails.Body.String())
	}
}

func TestAdminRegistrationScheduleStartsFromAvailableInventory(t *testing.T) {
	env := newIntegrationEnv(t)
	const runID = "run_scheduled_registration"
	var starts atomic.Int32
	var keepRunning atomic.Bool
	keepRunning.Store(true)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/start":
			starts.Add(1)
			var payload map[string]any
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			lines := strings.Split(fmt.Sprint(payload["accounts_text"]), "\n")
			if len(lines) != 3 {
				t.Errorf("scheduled task should use the three available accounts, got %#v", payload["accounts_text"])
			}
			_, _ = w.Write([]byte(`{"ok":true,"run_id":"` + runID + `","count":3}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/status":
			_, _ = fmt.Fprintf(w, `{"running":%t,"run_id":"%s","summary":{},"tasks":[]}`, keepRunning.Load(), runID)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/runs/"+runID+"/"):
			w.WriteHeader(http.StatusNotFound)
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	env.server.registrationBaseURL = upstream.URL
	env.server.registrationPollInterval = 10 * time.Millisecond
	env.server.registrationScheduleInterval = 10 * time.Millisecond
	t.Cleanup(func() { close(env.server.registrationSchedulerStop) })
	if err := env.store.SetCredential(t.Context(), registrationCredentialSource, "register-token"); err != nil {
		t.Fatal(err)
	}
	requireStatus(t, adminRequest(t, env, http.MethodPost, "/api/admin/mail-accounts/import", `{"platform":"mailcom","lines":"schedule1@mail.com----Pass1!\nschedule2@mail.com----Pass2!\nschedule3@mail.com----Pass3!"}`), http.StatusOK)

	saved := adminRequest(t, env, http.MethodPut, "/api/admin/registration/schedule", `{"enabled":true,"platform":"mailcom","count":5,"concurrency":2,"retryMax":1,"proxyMode":"configured","mailFastPath":false}`)
	requireStatus(t, saved, http.StatusOK)
	if !strings.Contains(saved.Body.String(), `"enabled":true`) || !strings.Contains(saved.Body.String(), `"count":5`) {
		t.Fatalf("unexpected schedule response: %s", saved.Body.String())
	}
	deadline := time.Now().Add(2 * time.Second)
	for starts.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if starts.Load() != 1 {
		t.Fatalf("scheduled registration did not start exactly once: %d", starts.Load())
	}
	time.Sleep(50 * time.Millisecond)
	if starts.Load() != 1 {
		t.Fatalf("scheduler started another task while the previous task was running: %d", starts.Load())
	}
	overview := adminRequest(t, env, http.MethodGet, "/api/admin/registration", "")
	requireStatus(t, overview, http.StatusOK)
	if !strings.Contains(overview.Body.String(), `"requestedCount":3`) || !strings.Contains(overview.Body.String(), `"enabled":true`) {
		t.Fatalf("scheduled task did not use current inventory: %s", overview.Body.String())
	}

	requireStatus(t, adminRequest(t, env, http.MethodPut, "/api/admin/registration/schedule", `{"enabled":false,"platform":"mailcom","count":5,"concurrency":2,"retryMax":1,"proxyMode":"configured","mailFastPath":false}`), http.StatusOK)
	keepRunning.Store(false)
	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		overview = adminRequest(t, env, http.MethodGet, "/api/admin/registration", "")
		if strings.Contains(overview.Body.String(), `"status":"completed"`) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !strings.Contains(overview.Body.String(), `"status":"completed"`) {
		t.Fatalf("scheduled registration did not finish: %s", overview.Body.String())
	}
}

func TestAdminChecksClaudeAccountsAliveInBulk(t *testing.T) {
	env := newIntegrationEnv(t)
	alive := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"alive@example.com","password":"pw","sessionKey":"sk-ant-sid02-alive","plan":"free","status":1}`)
	requireStatus(t, alive, http.StatusOK)
	dead := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"dead@example.com","password":"pw","sessionKey":"sk-ant-sid02-dead","plan":"free","status":1}`)
	requireStatus(t, dead, http.StatusOK)
	aliveID := numericID(t, responseData(t, alive), "id")
	deadID := numericID(t, responseData(t, dead), "id")

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/organizations" || r.Header.Get("Anthropic-Client-Platform") != "web_claude_ai" || r.Header.Get("Anthropic-Device-Id") == "" {
			t.Errorf("unexpected Claude check request: path=%s headers=%v", r.URL.Path, r.Header)
		}
		cookie, err := r.Cookie("sessionKey")
		if err != nil {
			t.Errorf("sessionKey cookie missing: %v", err)
			http.Error(w, "missing cookie", http.StatusUnauthorized)
			return
		}
		if cookie.Value != "sk-ant-sid02-alive" {
			http.Error(w, "expired", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[]`))
	}))
	defer upstream.Close()
	env.server.claudeBaseURL = upstream.URL

	checked := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts/check-alive", fmt.Sprintf(`{"ids":[%d,%d]}`, deadID, aliveID))
	requireStatus(t, checked, http.StatusOK)
	if !strings.Contains(checked.Body.String(), `"alive":1`) || !strings.Contains(checked.Body.String(), `"dead":1`) || !strings.Contains(checked.Body.String(), `"message":"存活"`) || !strings.Contains(checked.Body.String(), `"message":"HTTP 401"`) {
		t.Fatalf("unexpected account check result: %s", checked.Body.String())
	}
	accounts := adminRequest(t, env, http.MethodGet, "/api/admin/claude-accounts", "")
	requireStatus(t, accounts, http.StatusOK)
	if !strings.Contains(accounts.Body.String(), `"aliveStatus":"alive"`) || !strings.Contains(accounts.Body.String(), `"aliveStatus":"dead"`) || !strings.Contains(accounts.Body.String(), `"aliveCheckedAt":`) {
		t.Fatalf("account health was not persisted: %s", accounts.Body.String())
	}
	requireErrorCode(t, adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts/check-alive", `{"ids":[]}`), http.StatusBadRequest, "BAD_REQUEST")
}

func TestQbitVerificationAPIWithMockUpstream(t *testing.T) {
	env := newIntegrationEnv(t)
	admin, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const token = "ccm_qbit_integration"
	keyID, err := env.store.CreateAPIKey(t.Context(), "qbit", "ccm_qbit", tokenHash(token), admin.ID)
	if err != nil {
		t.Fatal(err)
	}
	cardID, err := env.store.CreateCard(t.Context(), dao.Card{Source: "qbit", CardID: "mock-channel-card", CardNo: "4333333333333333", ExpireMMYY: "1228", CCV: "123"})
	if err != nil {
		t.Fatal(err)
	}
	if err = env.store.SetCredential(t.Context(), "qbit", "mock-token"); err != nil {
		t.Fatal(err)
	}
	if _, err = env.store.DispatchCards(t.Context(), keyID, "qbit-request", "qbit", 1, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}

	var mode atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/quantum/card/budget-card/transaction/page" || r.Header.Get("Authorization") != "Bearer mock-token" || r.Header.Get("Fingerprint") != "fp-test" {
			t.Errorf("unexpected upstream request path=%s headers=%v", r.URL.Path, r.Header)
		}
		raw, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(raw), "mock-channel-card") || !strings.Contains(string(raw), `"size":200`) {
			t.Errorf("upstream request missed card id or page size: %s", raw)
		}
		switch mode.Load() {
		case 1:
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"records":[]}}`))
		case 2:
			http.Error(w, "upstream unavailable", http.StatusBadGateway)
		default:
			w.Header().Set("Content-Type", "application/json")
			_, _ = fmt.Fprintf(w, `{"code":200,"message":"ok","data":{"records":[{"id":"tx-1","transactionTime":%q,"cardId":"mock-channel-card","detail":"GOOGLE BMR 654321"}]}}`, time.Now().Format("2006-01-02 15:04:05"))
		}
	}))
	defer upstream.Close()
	env.server.qbitBaseURL = upstream.URL

	verify := func() *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/card/verify-code", strings.NewReader(fmt.Sprintf(`{"cardPoolId":%d,"googleRef":"BMR"}`, cardID)))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-API-Key", token)
		req.Header.Set("Fingerprint", "fp-test")
		resp := httptest.NewRecorder()
		env.router.ServeHTTP(resp, req)
		return resp
	}
	success := verify()
	requireStatus(t, success, http.StatusOK)
	if !strings.Contains(success.Body.String(), `"code":"654321"`) || !strings.Contains(success.Body.String(), `"status":"ok"`) {
		t.Fatalf("verification success response: %s", success.Body.String())
	}
	mode.Store(1)
	pending := verify()
	requireStatus(t, pending, http.StatusOK)
	if !strings.Contains(pending.Body.String(), `"status":"pending"`) {
		t.Fatalf("verification pending response: %s", pending.Body.String())
	}
	mode.Store(2)
	requireStatus(t, verify(), http.StatusBadGateway)
}

func TestAdminCreatesSlashCardAndImportsSecrets(t *testing.T) {
	env := newIntegrationEnv(t)
	if err := env.store.SetCredential(t.Context(), "slash_ccmax", "slash-create-key"); err != nil {
		t.Fatal(err)
	}
	createUpstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "slash-create-key" || r.Header.Get("X-Legal-Entity") != "entity-1" {
			t.Errorf("unexpected Slash create request: %s %s headers=%v", r.Method, r.URL.Path, r.Header)
		}
		if r.Method == http.MethodGet && r.URL.Path == "/card-group" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"items":[{"id":"ccmax","name":"Claude Max cards"}]}`))
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/card-product" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"items":[{"id":"cp_ccmax","prefix":"ccmax","status":"active"}]}`))
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/card/c_created" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"card":{"id":"c_created","name":"Claude card","last4":"0390","expiryMonth":"12","expiryYear":"2029","status":"active"}}`))
			return
		}
		if r.Method != http.MethodPost || r.URL.Path != "/card" {
			t.Errorf("unexpected Slash create request: %s %s", r.Method, r.URL.Path)
			http.NotFound(w, r)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["type"] != "virtual" || body["name"] != "Claude card" || body["accountId"] != "account-1" || body["cardGroupId"] != "ccmax" || body["cardProductId"] != "cp_ccmax" {
			t.Errorf("unexpected Slash create body: %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"c_created","name":"Claude card"}`))
	}))
	defer createUpstream.Close()
	var vaultCalls atomic.Int32
	vaultUpstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/card/c_created" || r.URL.Query().Get("include_pan") != "true" || r.URL.Query().Get("include_cvv") != "true" || r.Header.Get("X-API-Key") != "slash-create-key" {
			t.Errorf("unexpected Slash vault request: %s %s headers=%v", r.Method, r.URL.String(), r.Header)
		}
		w.Header().Set("Content-Type", "application/json")
		if vaultCalls.Add(1) == 1 {
			_, _ = w.Write([]byte(`{"data":{"id":"c_created"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"id":"c_created","pan":"4111 1111 1111 0390","cvv":"123"}}`))
	}))
	defer vaultUpstream.Close()
	env.server.slashBaseURL = createUpstream.URL
	env.server.slashVaultURL = vaultUpstream.URL
	env.server.slashImportRetryInterval = time.Millisecond

	created := adminRequest(t, env, http.MethodPost, "/api/admin/cards/slash-create", `{"source":"slash_ccmax","name":"Claude card","accountId":"account-1","cardGroupId":"ccmax","cardProductId":"ccmax","legalEntity":"entity-1"}`)
	requireStatus(t, created, http.StatusOK)
	if vaultCalls.Load() < 2 {
		t.Fatalf("Slash card import did not retry after incomplete Vault details")
	}
	localID := numericID(t, responseData(t, created), "id")
	card, err := env.store.CardByID(t.Context(), localID)
	if err != nil || card.Source != "slash_ccmax" || card.CardID != "c_created" || card.CardNo != "4111111111110390" || card.ExpireMMYY != "1229" || card.CCV != "123" || card.Status != 1 {
		t.Fatalf("Slash card was not imported: %#v %v", card, err)
	}
	if err = env.store.SetCredential(t.Context(), "slash_recovery", "slash-create-key"); err != nil {
		t.Fatal(err)
	}
	recovered := adminRequest(t, env, http.MethodPost, "/api/admin/cards/slash-import", `{"source":"slash_recovery","cardId":"c_created","legalEntity":"entity-1"}`)
	requireStatus(t, recovered, http.StatusOK)
	recoveredID := numericID(t, responseData(t, recovered), "id")
	recoveredCard, err := env.store.CardByID(t.Context(), recoveredID)
	if err != nil || recoveredCard.Source != "slash_recovery" || recoveredCard.CardID != "c_created" || recoveredCard.CardNo != "4111111111110390" || recoveredCard.ExpireMMYY != "1229" || recoveredCard.CCV != "123" {
		t.Fatalf("Slash card ID recovery import failed: %#v %v", recoveredCard, err)
	}
}

func TestCardDispatchAutoCreatesSlashCard(t *testing.T) {
	env := newIntegrationEnv(t)
	seedCoolingAndDisabledCards(t, env.store, "slash_auto")
	if err := env.store.SetCredential(t.Context(), "slash_auto", "slash-auto-key"); err != nil {
		t.Fatal(err)
	}
	var createCalls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "slash-auto-key" {
			t.Errorf("unexpected Slash API key: %q", r.Header.Get("X-API-Key"))
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/card":
			createCalls.Add(1)
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if body["cardGroupId"] != defaultSlashCardGroupID {
				t.Errorf("automatic Slash card group=%v want=%s", body["cardGroupId"], defaultSlashCardGroupID)
			}
			if name, _ := body["name"].(string); !strings.HasPrefix(name, automaticSlashCardPrefix+"-") {
				t.Errorf("automatic Slash card name=%q", name)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"id":"c_auto_created","name":"Automatic card"}`))
		case r.Method == http.MethodGet && r.URL.Path == "/card/c_auto_created" && r.URL.Query().Get("include_pan") == "true":
			_, _ = w.Write([]byte(`{"data":{"id":"c_auto_created","pan":"4111 1111 1111 0307","cvv":"307"}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/card/c_auto_created":
			_, _ = w.Write([]byte(`{"card":{"id":"c_auto_created","name":"Automatic card","expiryMonth":"03","expiryYear":"2030","status":"active"}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	env.server.slashBaseURL = upstream.URL
	env.server.slashVaultURL = upstream.URL
	env.server.slashImportRetryInterval = time.Millisecond

	keyResp := adminRequest(t, env, http.MethodPost, "/api/admin/api-keys", `{"name":"auto-card"}`)
	requireStatus(t, keyResp, http.StatusOK)
	apiToken, _ := responseData(t, keyResp)["key"].(string)
	req := httptest.NewRequest(http.MethodPost, "/api/card", strings.NewReader(`{"count":1}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", apiToken)
	req.Header.Set("Idempotency-Key", "auto-card-request")
	resp := httptest.NewRecorder()
	env.router.ServeHTTP(resp, req)

	requireStatus(t, resp, http.StatusOK)
	if createCalls.Load() != 1 || !strings.Contains(resp.Body.String(), `"source":"slash_auto"`) || !strings.Contains(resp.Body.String(), `"cardId":"c_auto_created"`) || !strings.Contains(resp.Body.String(), `"cardNo":"4111111111110307"`) {
		t.Fatalf("automatic card dispatch response=%s createCalls=%d", resp.Body.String(), createCalls.Load())
	}

	retry := httptest.NewRequest(http.MethodPost, "/api/card", strings.NewReader(`{"count":1}`))
	retry.Header.Set("Content-Type", "application/json")
	retry.Header.Set("X-API-Key", apiToken)
	retry.Header.Set("Idempotency-Key", "auto-card-request")
	retryResp := httptest.NewRecorder()
	env.router.ServeHTTP(retryResp, retry)
	requireStatus(t, retryResp, http.StatusOK)
	if createCalls.Load() != 1 || retryResp.Body.String() != resp.Body.String() {
		t.Fatalf("idempotent retry changed automatic card: first=%s retry=%s createCalls=%d", resp.Body.String(), retryResp.Body.String(), createCalls.Load())
	}
}

func TestCardDispatchReturnsInsufficientCardsWhenAutoCreateFails(t *testing.T) {
	env := newIntegrationEnv(t)
	seedCoolingAndDisabledCards(t, env.store, "slash")
	keyResp := adminRequest(t, env, http.MethodPost, "/api/admin/api-keys", `{"name":"auto-card-failure"}`)
	requireStatus(t, keyResp, http.StatusOK)
	apiToken, _ := responseData(t, keyResp)["key"].(string)

	resp := requestWithAPIKey(t, env.router, http.MethodPost, "/api/card", apiToken, `{"count":1}`)
	requireErrorCode(t, resp, http.StatusConflict, "INSUFFICIENT_CARDS")
	if resp.Body.String() != `{"code":"INSUFFICIENT_CARDS","message":"insufficient cards: available=0 requested=1"}` {
		t.Fatalf("unexpected insufficient-card response: %s", resp.Body.String())
	}
}

func TestCreatedSlashCardImportTimesOutWhileDetailsAreIncomplete(t *testing.T) {
	env := newIntegrationEnv(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"c_pending"}}`))
	}))
	defer upstream.Close()
	env.server.slashBaseURL = upstream.URL
	env.server.slashVaultURL = upstream.URL
	env.server.slashImportTimeout = 20 * time.Millisecond
	env.server.slashImportRetryInterval = time.Millisecond

	result, err := env.server.importCreatedSlashCard(t.Context(), "slash", "slash-key", "", "c_pending")
	detail := ""
	if err != nil {
		detail = err.Error()
	}
	pendingReason := strings.Contains(detail, "missing PAN, CVV, expiry") || strings.Contains(detail, "context deadline exceeded")
	if result != nil || err == nil || !strings.Contains(detail, "timed out after 20ms") || !pendingReason {
		t.Fatalf("unexpected pending Slash import result=%#v err=%v", result, err)
	}
}

func TestSlashVerificationAPIWithMockUpstream(t *testing.T) {
	env := newIntegrationEnv(t)
	admin, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const token = "ccm_slash_integration"
	keyID, err := env.store.CreateAPIKey(t.Context(), "slash", "ccm_slash", tokenHash(token), admin.ID)
	if err != nil {
		t.Fatal(err)
	}
	cardPoolID, err := env.store.CreateCard(t.Context(), dao.Card{Source: "slash", CardID: "slash-card-123", CardNo: "4444444444444444", ExpireMMYY: "1228", CCV: "123"})
	if err != nil {
		t.Fatal(err)
	}
	if err = env.store.SetCredential(t.Context(), "slash", "slash-api-key"); err != nil {
		t.Fatal(err)
	}
	if _, err = env.store.DispatchCards(t.Context(), keyID, "slash-request", "slash", 1, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/card/slash-card-123/events" || r.Header.Get("X-API-Key") != "slash-api-key" {
			t.Errorf("unexpected Slash request path=%s headers=%v", r.URL.Path, r.Header)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"items":[{"createdAt":%q,"merchantData":{"description":"GOOGLE BMR 789012"}}]}`, time.Now().UTC().Format(time.RFC3339))
	}))
	defer upstream.Close()
	env.server.slashBaseURL = upstream.URL

	response := requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/verify-code", token, fmt.Sprintf(`{"cardPoolId":%d,"googleRef":"BMR"}`, cardPoolID))
	requireStatus(t, response, http.StatusOK)
	if !strings.Contains(response.Body.String(), `"code":"789012"`) || !strings.Contains(response.Body.String(), `"status":"ok"`) {
		t.Fatalf("Slash verification response: %s", response.Body.String())
	}
	history := adminRequest(t, env, http.MethodGet, fmt.Sprintf("/api/admin/cards/%d/history", cardPoolID), "")
	requireStatus(t, history, http.StatusOK)
	historyRaw, ok := responseData(t, history)["raw"].(string)
	if !ok || !strings.Contains(historyRaw, `"merchantData":{"description":"GOOGLE BMR 789012"}`) {
		t.Fatalf("Slash raw history response: %s", history.Body.String())
	}
}

func TestAPIValidationAndErrorContracts(t *testing.T) {
	env := newIntegrationEnv(t)

	admin, err := env.store.AdminByUsername(t.Context(), "root")
	if err != nil {
		t.Fatal(err)
	}
	const apiToken = "ccm_validation"
	if _, err = env.store.CreateAPIKey(t.Context(), "validation", "ccm_valid", tokenHash(apiToken), admin.ID); err != nil {
		t.Fatal(err)
	}
	wrongCredentialKey := requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/verify-code/token", "bad-key", `{"source":"qbit","token":"new-qbit-token"}`)
	requireErrorCode(t, wrongCredentialKey, http.StatusUnauthorized, "INVALID_API_KEY")
	credentialUpload := requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/verify-code/token", apiToken, `{"source":"qbit","token":"new-qbit-token"}`)
	requireStatus(t, credentialUpload, http.StatusOK)
	if strings.Contains(credentialUpload.Body.String(), "new-qbit-token") || !strings.Contains(credentialUpload.Body.String(), `"updated":true`) {
		t.Fatalf("credential upload leaked token or missed result: %s", credentialUpload.Body.String())
	}
	storedToken, err := env.store.Credential(t.Context(), "qbit")
	if err != nil || storedToken != "new-qbit-token" {
		t.Fatalf("uploaded credential not stored: token=%q err=%v", storedToken, err)
	}
	slashCredentialUpload := requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/verify-code/token", apiToken, `{"source":"slash","token":"slash-api-key"}`)
	requireStatus(t, slashCredentialUpload, http.StatusOK)
	if slashToken, slashErr := env.store.Credential(t.Context(), "slash"); slashErr != nil || slashToken != "slash-api-key" {
		t.Fatalf("uploaded Slash credential not stored: token=%q err=%v", slashToken, slashErr)
	}

	malformed := adminRequest(t, env, http.MethodPost, "/api/admin/cards", `{`)
	requireErrorCode(t, malformed, http.StatusBadRequest, "BAD_REQUEST")
	missingOrderInventory := adminRequest(t, env, http.MethodPost, "/api/admin/orders", `{"batchNo":"no-stock","buyer":"buyer","quantity":1,"plan":"free"}`)
	requireErrorCode(t, missingOrderInventory, http.StatusConflict, "INSUFFICIENT_ACCOUNTS")
	duplicateOne := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"duplicate@example.com","password":"pw","sessionKey":"duplicate-sk","plan":"free","status":1}`)
	requireStatus(t, duplicateOne, http.StatusOK)
	duplicateTwo := adminRequest(t, env, http.MethodPost, "/api/admin/claude-accounts", `{"mail":"duplicate@example.com","password":"pw","sessionKey":"another-sk","plan":"free","status":1}`)
	requireErrorCode(t, duplicateTwo, http.StatusConflict, "DUPLICATE")

	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/add", "bad-key", `{}`), http.StatusUnauthorized, "INVALID_API_KEY")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/add", apiToken, `{}`), http.StatusBadRequest, "BAD_REQUEST")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account", apiToken, `{"count":101}`), http.StatusBadRequest, "INVALID_COUNT")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account", apiToken, `{"count":1,"plan":"max_20x"}`), http.StatusBadRequest, "INVALID_PLAN")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/release", apiToken, `{"requestId":"","mails":[]}`), http.StatusBadRequest, "BAD_REQUEST")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/upgrade", apiToken, `{"mail":"duplicate@example.com","plan":"free","cardPoolId":1}`), http.StatusBadRequest, "OPERATION_FAILED")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/claude_account/upgrade", apiToken, `{"mail":"duplicate@example.com","plan":"max_20x"}`), http.StatusBadRequest, "BAD_REQUEST")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/card", apiToken, `{"count":101}`), http.StatusBadRequest, "INVALID_COUNT")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/report", apiToken, `{"requestId":"","cards":[]}`), http.StatusBadRequest, "BAD_REQUEST")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/google_account/report", apiToken, `{"requestId":"request","googleAccountId":1,"status":"unknown"}`), http.StatusBadRequest, "OPERATION_FAILED")
	requireErrorCode(t, requestWithAPIKey(t, env.router, http.MethodPost, "/api/card/verify-code", apiToken, `{"cardPoolId":0,"googleRef":""}`), http.StatusBadRequest, "BAD_REQUEST")
}
