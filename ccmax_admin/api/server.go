package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"ccmax/conf"
	"ccmax/dao"

	"github.com/gin-gonic/gin"
	"golang.org/x/crypto/bcrypt"
)

const sessionCookie = "ccmax_session"

type Server struct {
	store                        *dao.Store
	cfg                          conf.Config
	qbitBaseURL                  string
	slashBaseURL                 string
	slashVaultURL                string
	slashImportTimeout           time.Duration
	slashImportRetryInterval     time.Duration
	claudeBaseURL                string
	registrationBaseURL          string
	registrationHTTPClient       *http.Client
	registrationPollInterval     time.Duration
	registrationMu               sync.Mutex
	registrationRun              *registrationRunState
	registrationStarting         bool
	registrationScheduleInterval time.Duration
	registrationSchedulerOnce    sync.Once
	registrationSchedulerStop    chan struct{}
	chatGPTRedeemBaseURL         string
	chatGPTRedeemAPIKey          string
	chatGPTRedeemHTTPClient      *http.Client
}

func New(store *dao.Store, cfg conf.Config) *Server {
	registrationBaseURL := strings.TrimSpace(cfg.ClaudeRegisterURL)
	if registrationBaseURL == "" {
		registrationBaseURL = "http://claude-register:8000"
	}
	return &Server{
		store: store, cfg: cfg,
		qbitBaseURL: qbitBaseURL, slashBaseURL: slashBaseURL, slashVaultURL: slashVaultBaseURL,
		slashImportTimeout: 30 * time.Second, slashImportRetryInterval: time.Second, claudeBaseURL: claudeBaseURL,
		registrationBaseURL: registrationBaseURL, registrationHTTPClient: &http.Client{Timeout: 30 * time.Second}, registrationPollInterval: 3 * time.Second,
		registrationScheduleInterval: time.Minute, registrationSchedulerStop: make(chan struct{}),
		chatGPTRedeemBaseURL: strings.TrimRight(strings.TrimSpace(cfg.ChatGPTRedeemURL), "/"),
		chatGPTRedeemAPIKey:  strings.TrimSpace(cfg.ChatGPTRedeemKey), chatGPTRedeemHTTPClient: &http.Client{Timeout: 30 * time.Second},
	}
}

func (s *Server) Bootstrap() error {
	ctx := context.Background()
	n, err := s.store.AdminCount(ctx)
	if err != nil {
		return err
	}
	if n == 0 {
		if len(s.cfg.BootstrapPass) < 8 {
			return errors.New("bootstrap admin password must contain at least 8 characters")
		}
		hash, hashErr := bcrypt.GenerateFromPassword([]byte(s.cfg.BootstrapPass), bcrypt.DefaultCost)
		if hashErr != nil {
			return hashErr
		}
		if _, err = s.store.CreateAdmin(ctx, s.cfg.BootstrapUser, string(hash), "系统管理员", "super_admin"); err != nil {
			return err
		}
	}
	s.resumeRegistrationMonitor()
	if schedule, scheduleErr := s.store.RegistrationSchedule(ctx); scheduleErr == nil && schedule.Enabled {
		s.startRegistrationScheduler()
	}
	return nil
}

func (s *Server) Setup(r *gin.Engine) {
	r.GET("/api/health", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })
	r.POST("/api/chatgpt/redeem/check", s.checkChatGPTCDK)
	r.POST("/api/chatgpt/redeem/submit", s.redeemChatGPTCDK)
	r.POST("/api/chatgpt/redeem/task", s.getPublicChatGPTRedeemTask)
	r.POST("/api/admin/auth/login", s.login)
	admin := r.Group("/api/admin")
	admin.Use(s.adminAuth())
	admin.POST("/auth/logout", s.logout)
	admin.GET("/auth/me", s.me)
	admin.POST("/auth/change-password", s.changePassword)
	admin.GET("/dashboard", s.dashboard)
	admin.GET("/claude-accounts", s.listAccounts)
	admin.POST("/claude-accounts", s.createAccount)
	admin.POST("/claude-accounts/import", s.importAccounts)
	admin.PUT("/claude-accounts/:id", s.updateAccount)
	admin.DELETE("/claude-accounts/:id", s.deleteAccount)
	admin.POST("/claude-accounts/:id/reset", s.resetAccount)
	admin.PATCH("/claude-accounts/:id/status", s.accountStatus)
	admin.POST("/claude-accounts/:id/release", s.releaseAccountLease)
	admin.POST("/claude-accounts/check-alive", s.checkAccountsAlive)
	admin.GET("/google-accounts", s.listGoogleAccounts)
	admin.POST("/google-accounts/import", s.importGoogleAccounts)
	admin.PATCH("/google-accounts/:id/status", s.googleAccountStatus)
	admin.DELETE("/google-accounts/:id", s.deleteGoogleAccount)
	admin.GET("/mail-accounts", s.listMailAccounts)
	admin.POST("/mail-accounts/import", s.importMailAccounts)
	admin.PATCH("/mail-accounts/:id/status", s.mailAccountStatus)
	admin.DELETE("/mail-accounts/:id", s.deleteMailAccount)
	admin.GET("/registration", s.registrationOverview)
	admin.POST("/registration/start", s.startRegistration)
	admin.POST("/registration/stop", s.stopRegistration)
	admin.PUT("/registration/schedule", s.updateRegistrationSchedule)
	admin.GET("/cards", s.listCards)
	admin.POST("/cards", s.createCard)
	admin.POST("/cards/import", s.importCards)
	admin.POST("/cards/slash-create", s.createSlashCard)
	admin.POST("/cards/slash-import", s.importSlashCardByID)
	admin.PUT("/cards/:id", s.updateCard)
	admin.PATCH("/cards/:id/status", s.cardStatus)
	admin.GET("/cards/:id/history", s.cardHistory)
	admin.GET("/channel-credentials", s.listCredentials)
	admin.PUT("/channel-credentials/:source", s.setCredential)
	admin.GET("/orders", s.listOrders)
	admin.POST("/orders", s.createOrder)
	admin.GET("/orders/:id", s.getOrder)
	admin.GET("/orders/:id/download", s.downloadOrder)
	admin.POST("/orders/:id/cancel", s.cancelOrder)
	admin.GET("/api-keys", s.listAPIKeys)
	admin.POST("/api-keys", s.createAPIKey)
	admin.PATCH("/api-keys/:id/status", s.apiKeyStatus)
	admin.DELETE("/api-keys/:id", s.deleteAPIKey)
	admin.GET("/chatgpt-cdks", s.listChatGPTCDKs)
	admin.POST("/chatgpt-cdks/generate", s.generateChatGPTCDKs)
	admin.GET("/chatgpt-cdks/export", s.exportChatGPTCDKs)
	admin.GET("/chatgpt-tasks", s.listChatGPTTasks)
	admin.GET("/chatgpt-tasks/:id", s.getChatGPTTask)
	admins := admin.Group("/admin-users")
	admins.Use(requireSuperAdmin())
	admins.GET("", s.listAdmins)
	admins.POST("", s.createAdmin)
	admins.PUT("/:id", s.updateAdmin)
	public := r.Group("/api")
	public.Use(s.apiKeyAuth())
	public.POST("/claude_account/add", s.addFreeAccounts)
	public.POST("/claude_account", s.dispatchAccounts)
	public.POST("/claude_account/release", s.releaseDispatchedAccounts)
	public.POST("/claude_account/upgrade", s.upgradeAccount)
	public.POST("/google_account", s.dispatchGoogleAccount)
	public.POST("/google_account/report", s.reportGoogleAccountUsed)
	public.POST("/mail_account", s.dispatchMailAccount)
	public.POST("/mail_account/report", s.reportMailAccountUsed)
	public.POST("/card", s.dispatchCards)
	public.POST("/card/report", s.reportCards)
	public.POST("/card/verify-code/token", s.setVerifyCodeToken)
	public.POST("/card/verify-code", s.verifyCode)
	public.POST("/chatgpt/cdk/check", s.checkChatGPTCDK)
	public.POST("/chatgpt/cdk/redeem", s.redeemChatGPTCDK)
	public.GET("/chatgpt/cdk/tasks/:taskId", s.getChatGPTRedeemTask)
}

func randomToken(bytes int) string {
	b := make([]byte, bytes)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}
func tokenHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}
func clientIP(c *gin.Context) string { return c.ClientIP() }
func page(c *gin.Context) (int, int) {
	p, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	z, _ := strconv.Atoi(c.DefaultQuery("size", "20"))
	if p < 1 {
		p = 1
	}
	if z < 1 {
		z = 20
	}
	if z > 200 {
		z = 200
	}
	return p, z
}
func idParam(c *gin.Context) (int64, error) { return strconv.ParseInt(c.Param("id"), 10, 64) }
func ok(c *gin.Context, data any)           { c.JSON(http.StatusOK, gin.H{"data": data}) }
func fail(c *gin.Context, status int, code string, err error) {
	c.JSON(status, gin.H{"code": code, "message": err.Error()})
}
func bind(c *gin.Context, dst any) bool {
	if err := c.ShouldBindJSON(dst); err != nil {
		fail(c, 400, "BAD_REQUEST", err)
		return false
	}
	return true
}
func handleStoreError(c *gin.Context, err error) {
	switch {
	case dao.IsNoRows(err):
		fail(c, 404, "NOT_FOUND", err)
	case dao.IsUniqueError(err):
		fail(c, 409, "DUPLICATE", err)
	case strings.Contains(err.Error(), "insufficient accounts"):
		fail(c, 409, "INSUFFICIENT_ACCOUNTS", err)
	case strings.Contains(err.Error(), "insufficient google accounts"):
		fail(c, 409, "INSUFFICIENT_GOOGLE_ACCOUNTS", err)
	case strings.Contains(err.Error(), "insufficient mail accounts"):
		fail(c, 409, "INSUFFICIENT_MAIL_ACCOUNTS", err)
	case strings.Contains(err.Error(), "insufficient cards"):
		fail(c, 409, "INSUFFICIENT_CARDS", err)
	case strings.Contains(err.Error(), "insufficient chatgpt cdks"):
		fail(c, 409, "INSUFFICIENT_CHATGPT_CDKS", err)
	case strings.Contains(err.Error(), "idempotency"):
		fail(c, 409, "IDEMPOTENCY_CONFLICT", err)
	case strings.Contains(err.Error(), "lease"):
		fail(c, 409, "LEASE_CONFLICT", err)
	default:
		fail(c, 400, "OPERATION_FAILED", err)
	}
}

func (s *Server) login(c *gin.Context) {
	var req struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if !bind(c, &req) {
		return
	}
	a, err := s.store.AdminByUsername(c.Request.Context(), req.Username)
	if err != nil || a.Status != 1 || bcrypt.CompareHashAndPassword([]byte(a.PasswordHash), []byte(req.Password)) != nil {
		fail(c, 401, "INVALID_CREDENTIALS", errors.New("用户名或密码错误"))
		return
	}
	token := randomToken(32)
	expires := time.Now().Add(s.cfg.SessionTTL)
	if err = s.store.CreateSession(c.Request.Context(), a.ID, tokenHash(token), clientIP(c), c.Request.UserAgent(), expires); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.TouchLogin(c.Request.Context(), a.ID)
	http.SetCookie(c.Writer, &http.Cookie{Name: sessionCookie, Value: token, Path: "/", HttpOnly: true, Secure: s.cfg.CookieSecure, SameSite: http.SameSiteLaxMode, Expires: expires})
	s.store.Audit(c.Request.Context(), "admin", a.ID, "login", "session", "", "{}", clientIP(c))
	ok(c, a)
}
func (s *Server) logout(c *gin.Context) {
	if cookie, err := c.Cookie(sessionCookie); err == nil {
		_ = s.store.DeleteSession(c.Request.Context(), tokenHash(cookie))
	}
	http.SetCookie(c.Writer, &http.Cookie{Name: sessionCookie, Value: "", Path: "/", HttpOnly: true, MaxAge: -1, SameSite: http.SameSiteLaxMode})
	ok(c, true)
}
func (s *Server) me(c *gin.Context) { ok(c, currentAdmin(c)) }
func (s *Server) adminAuth() gin.HandlerFunc {
	return func(c *gin.Context) {
		token, err := c.Cookie(sessionCookie)
		if err != nil {
			fail(c, 401, "UNAUTHORIZED", errors.New("请先登录"))
			c.Abort()
			return
		}
		a, err := s.store.AdminBySession(c.Request.Context(), tokenHash(token))
		if err != nil {
			fail(c, 401, "UNAUTHORIZED", errors.New("登录已失效"))
			c.Abort()
			return
		}
		c.Set("admin", a)
		c.Next()
	}
}
func currentAdmin(c *gin.Context) *dao.Admin {
	a, _ := c.Get("admin")
	admin, _ := a.(*dao.Admin)
	return admin
}
func requireSuperAdmin() gin.HandlerFunc {
	return func(c *gin.Context) {
		a := currentAdmin(c)
		if a == nil || a.Role != "super_admin" {
			fail(c, 403, "FORBIDDEN", errors.New("需要超级管理员权限"))
			c.Abort()
			return
		}
		c.Next()
	}
}
func (s *Server) changePassword(c *gin.Context) {
	var req struct {
		CurrentPassword string `json:"currentPassword"`
		NewPassword     string `json:"newPassword"`
	}
	if !bind(c, &req) {
		return
	}
	a := currentAdmin(c)
	full, err := s.store.AdminByID(c.Request.Context(), a.ID)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	if bcrypt.CompareHashAndPassword([]byte(full.PasswordHash), []byte(req.CurrentPassword)) != nil {
		fail(c, 400, "INVALID_PASSWORD", errors.New("当前密码错误"))
		return
	}
	if len(req.NewPassword) < 8 {
		fail(c, 400, "WEAK_PASSWORD", errors.New("新密码至少 8 位"))
		return
	}
	hash, _ := bcrypt.GenerateFromPassword([]byte(req.NewPassword), bcrypt.DefaultCost)
	if err = s.store.UpdateAdmin(c.Request.Context(), a.ID, a.DisplayName, a.Role, a.Status, string(hash)); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}
func (s *Server) dashboard(c *gin.Context) {
	data, err := s.store.Dashboard(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, data)
}

func (s *Server) listAccounts(c *gin.Context) {
	p, z := page(c)
	status, _ := strconv.Atoi(c.Query("status"))
	items, total, err := s.store.ListAccounts(c.Request.Context(), p, z, c.Query("q"), c.Query("plan"), status)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total})
}
func (s *Server) createAccount(c *gin.Context) {
	var a dao.ClaudeAccount
	if !bind(c, &a) {
		return
	}
	id, err := s.store.CreateAccount(c.Request.Context(), a)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "create", "claude_account", strconv.FormatInt(id, 10), "{}", clientIP(c))
	ok(c, gin.H{"id": id})
}
func (s *Server) importAccounts(c *gin.Context) {
	var req struct {
		Accounts []dao.ClaudeAccount `json:"accounts"`
	}
	if !bind(c, &req) {
		return
	}
	created, duplicates := 0, 0
	errs := []string{}
	for i, a := range req.Accounts {
		_, err := s.store.CreateAccount(c.Request.Context(), a)
		if err == nil {
			created++
		} else if dao.IsUniqueError(err) {
			duplicates++
		} else {
			errs = append(errs, fmt.Sprintf("第 %d 行: %v", i+1, err))
		}
	}
	ok(c, gin.H{"created": created, "duplicates": duplicates, "errors": errs})
}
func (s *Server) updateAccount(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	var a dao.ClaudeAccount
	if !bind(c, &a) {
		return
	}
	if err = s.store.UpdateAccount(c.Request.Context(), id, a); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}
func (s *Server) accountStatus(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	var req struct {
		Status int `json:"status"`
	}
	if !bind(c, &req) {
		return
	}
	if err = s.store.SetAccountStatus(c.Request.Context(), id, req.Status); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}

func (s *Server) deleteAccount(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	if err = s.store.DeleteAccount(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "delete", "claude_account", strconv.FormatInt(id, 10), "{}", clientIP(c))
	ok(c, true)
}

func (s *Server) resetAccount(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	if err = s.store.ResetAccount(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "reset", "claude_account", strconv.FormatInt(id, 10), "{}", clientIP(c))
	ok(c, true)
}

func (s *Server) releaseAccountLease(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	if err = s.store.ReleaseAccountLease(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "release_lease", "claude_account", strconv.FormatInt(id, 10), "{}", clientIP(c))
	ok(c, true)
}

func (s *Server) listCards(c *gin.Context) {
	p, z := page(c)
	status, _ := strconv.Atoi(c.Query("status"))
	items, total, err := s.store.ListCards(c.Request.Context(), p, z, c.Query("q"), c.Query("source"), status)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	stats, err := s.store.CardStats(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total, "stats": stats})
}
func (s *Server) createCard(c *gin.Context) {
	var item dao.Card
	if !bind(c, &item) {
		return
	}
	id, err := s.store.CreateCard(c.Request.Context(), item)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"id": id})
}
func (s *Server) updateCard(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	var item dao.Card
	if !bind(c, &item) {
		return
	}
	if err = s.store.UpdateCard(c.Request.Context(), id, item); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}
func (s *Server) cardStatus(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	var req struct {
		Status int `json:"status"`
	}
	if !bind(c, &req) {
		return
	}
	if err = s.store.SetCardStatus(c.Request.Context(), id, req.Status); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}
func (s *Server) importCards(c *gin.Context) {
	var req struct {
		Source string `json:"source"`
		Lines  string `json:"lines"`
	}
	if !bind(c, &req) {
		return
	}
	created, duplicates := 0, 0
	errs := []string{}
	for i, line := range strings.Split(req.Lines, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		card, err := parseImportedCard(req.Source, line)
		if err != nil {
			errs = append(errs, fmt.Sprintf("第 %d 行格式错误", i+1))
			continue
		}
		_, err = s.store.CreateCard(c.Request.Context(), card)
		if err == nil {
			created++
		} else if dao.IsUniqueError(err) {
			duplicates++
		} else {
			errs = append(errs, fmt.Sprintf("第 %d 行: %v", i+1, err))
		}
	}
	ok(c, gin.H{"created": created, "duplicates": duplicates, "errors": errs})
}

func parseImportedCard(source, line string) (dao.Card, error) {
	parts := strings.Fields(line)
	if len(parts) < 4 {
		return dao.Card{}, errors.New("not enough fields")
	}
	for expiryIndex := 1; expiryIndex < len(parts)-2; expiryIndex++ {
		expiry := strings.ReplaceAll(parts[expiryIndex], "/", "")
		if len(expiry) != 4 {
			continue
		}
		if _, err := strconv.Atoi(expiry); err != nil {
			continue
		}
		ccv := parts[expiryIndex+1]
		if (len(ccv) != 3 && len(ccv) != 4) || !allDigits(ccv) {
			continue
		}
		return dao.Card{
			Source:     source,
			CardNo:     parts[0],
			ExpireMMYY: expiry,
			CCV:        ccv,
			CardID:     strings.Join(parts[expiryIndex+2:], " "),
		}, nil
	}
	return dao.Card{}, errors.New("expiry and ccv not found")
}

func allDigits(value string) bool {
	if value == "" {
		return false
	}
	for _, char := range value {
		if char < '0' || char > '9' {
			return false
		}
	}
	return true
}
func (s *Server) listCredentials(c *gin.Context) {
	items, err := s.store.ListCredentials(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, items)
}
func (s *Server) setCredential(c *gin.Context) {
	var req struct {
		Token string `json:"token"`
	}
	if !bind(c, &req) {
		return
	}
	if err := s.store.SetCredential(c.Request.Context(), c.Param("source"), req.Token); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}

func (s *Server) setVerifyCodeToken(c *gin.Context) {
	var req struct {
		Source string `json:"source"`
		Token  string `json:"token"`
	}
	if !bind(c, &req) {
		return
	}
	req.Source = strings.ToLower(strings.TrimSpace(req.Source))
	req.Token = strings.TrimSpace(req.Token)
	if req.Source == "" || req.Token == "" {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("source and token are required"))
		return
	}
	if !strings.HasPrefix(req.Source, "qbit") && !strings.HasPrefix(req.Source, "slash") {
		fail(c, http.StatusBadRequest, "UNSUPPORTED_SOURCE", errors.New("only qbit and slash credentials are supported"))
		return
	}
	if err := s.store.SetCredential(c.Request.Context(), req.Source, req.Token); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "api_key", currentAPIKey(c).ID, "set_verify_code_token", "channel_credential", req.Source, `{"tokenUpdated":true}`, clientIP(c))
	ok(c, gin.H{"source": req.Source, "updated": true})
}

func (s *Server) listOrders(c *gin.Context) {
	p, z := page(c)
	items, total, err := s.store.ListOrders(c.Request.Context(), p, z, c.Query("q"), c.Query("productType"), c.Query("plan"), c.Query("cdkSku"), c.Query("status"))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total})
}
func (s *Server) createOrder(c *gin.Context) {
	var o dao.Order
	if !bind(c, &o) {
		return
	}
	if strings.TrimSpace(o.BatchNo) == "" {
		o.BatchNo = "ORD-" + time.Now().Format("20060102-150405") + "-" + strings.ToUpper(randomToken(3))
	}
	created, err := s.store.CreateOrder(c.Request.Context(), o, currentAdmin(c).ID)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "create", "order", strconv.FormatInt(created.ID, 10), "{}", clientIP(c))
	ok(c, created)
}
func (s *Server) getOrder(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	o, err := s.store.OrderByID(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	accounts, err := s.store.OrderAccounts(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	cdks, err := s.store.OrderCDKs(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"order": o, "accounts": accounts, "cdks": cdks})
}
func (s *Server) downloadOrder(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	o, err := s.store.OrderByID(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	if o.Status != "allocated" {
		fail(c, 400, "NOT_ALLOCATED", errors.New("订单未分配"))
		return
	}
	var b strings.Builder
	if o.ProductType == "chatgpt_cdk" {
		items, loadErr := s.store.OrderCDKs(c.Request.Context(), id)
		if loadErr != nil {
			handleStoreError(c, loadErr)
			return
		}
		for _, item := range items {
			fmt.Fprintln(&b, item.Code)
		}
	} else {
		items, loadErr := s.store.OrderAccounts(c.Request.Context(), id)
		if loadErr != nil {
			handleStoreError(c, loadErr)
			return
		}
		for _, a := range items {
			fmt.Fprintf(&b, "%s----%s----%s\n", a.Mail, a.Password, a.SessionKey)
		}
	}
	filename := strings.Map(func(r rune) rune {
		if r == '/' || r == '\\' || r == '\n' || r == '\r' {
			return '-'
		}
		return r
	}, o.BatchNo) + ".txt"
	c.Header("Content-Type", "text/plain; charset=utf-8")
	c.Header("Content-Disposition", fmt.Sprintf(`attachment; filename="%s"`, filename))
	_ = s.store.RecordOrderDownload(c.Request.Context(), id, currentAdmin(c).ID, clientIP(c))
	c.String(200, b.String())
}
func (s *Server) cancelOrder(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	if err = s.store.CancelOrder(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}

func (s *Server) listAPIKeys(c *gin.Context) {
	items, err := s.store.ListAPIKeys(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, items)
}
func (s *Server) createAPIKey(c *gin.Context) {
	var req struct {
		Name string `json:"name"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.Name) == "" {
		fail(c, 400, "BAD_REQUEST", errors.New("name is required"))
		return
	}
	token := "ccm_" + randomToken(24)
	prefix := token[:12]
	id, err := s.store.CreateAPIKey(c.Request.Context(), req.Name, prefix, tokenHash(token), currentAdmin(c).ID)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"id": id, "key": token, "prefix": prefix})
}
func (s *Server) apiKeyStatus(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	var req struct {
		Status int `json:"status"`
	}
	if !bind(c, &req) {
		return
	}
	if err = s.store.SetAPIKeyStatus(c.Request.Context(), id, req.Status); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}
func (s *Server) deleteAPIKey(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	if err = s.store.DeleteAPIKey(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "delete", "api_key", strconv.FormatInt(id, 10), `{"softDelete":true}`, clientIP(c))
	ok(c, true)
}

func (s *Server) listAdmins(c *gin.Context) {
	items, err := s.store.ListAdmins(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, items)
}
func (s *Server) createAdmin(c *gin.Context) {
	var req struct {
		Username    string `json:"username"`
		Password    string `json:"password"`
		DisplayName string `json:"displayName"`
		Role        string `json:"role"`
	}
	if !bind(c, &req) {
		return
	}
	if len(req.Password) < 8 {
		fail(c, 400, "WEAK_PASSWORD", errors.New("密码至少 8 位"))
		return
	}
	hash, _ := bcrypt.GenerateFromPassword([]byte(req.Password), bcrypt.DefaultCost)
	id, err := s.store.CreateAdmin(c.Request.Context(), req.Username, string(hash), req.DisplayName, req.Role)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"id": id})
}
func (s *Server) updateAdmin(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	var req struct {
		DisplayName string `json:"displayName"`
		Role        string `json:"role"`
		Status      int    `json:"status"`
		Password    string `json:"password"`
	}
	if !bind(c, &req) {
		return
	}
	if id == currentAdmin(c).ID && req.Status == -1 {
		fail(c, 400, "SELF_DISABLE", errors.New("不能禁用当前管理员"))
		return
	}
	target, err := s.store.AdminByID(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	if target.Role == "super_admin" && target.Status == 1 && (req.Role != "super_admin" || req.Status != 1) {
		n, _ := s.store.ActiveSuperAdminCount(c.Request.Context())
		if n <= 1 {
			fail(c, 400, "LAST_SUPER_ADMIN", errors.New("必须保留一个启用的超级管理员"))
			return
		}
	}
	hash := ""
	if req.Password != "" {
		if len(req.Password) < 8 {
			fail(c, 400, "WEAK_PASSWORD", errors.New("密码至少 8 位"))
			return
		}
		b, _ := bcrypt.GenerateFromPassword([]byte(req.Password), bcrypt.DefaultCost)
		hash = string(b)
	}
	if err = s.store.UpdateAdmin(c.Request.Context(), id, req.DisplayName, req.Role, req.Status, hash); err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, true)
}

func (s *Server) apiKeyAuth() gin.HandlerFunc {
	return func(c *gin.Context) {
		token := strings.TrimSpace(c.GetHeader("X-API-Key"))
		if token == "" {
			auth := c.GetHeader("Authorization")
			token = strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
		}
		key, err := s.store.APIKeyByHash(c.Request.Context(), tokenHash(token))
		if token == "" || err != nil {
			fail(c, 401, "INVALID_API_KEY", errors.New("无效的 API Key"))
			c.Abort()
			return
		}
		c.Set("apiKey", key)
		s.store.TouchAPIKey(c.Request.Context(), key.ID)
		c.Next()
	}
}
func currentAPIKey(c *gin.Context) *dao.APIKey {
	v, _ := c.Get("apiKey")
	k, _ := v.(*dao.APIKey)
	return k
}

func (s *Server) addFreeAccounts(c *gin.Context) {
	var req struct {
		Mail       string `json:"mail"`
		Password   string `json:"password"`
		SessionKey string `json:"sessionKey"`
		Accounts   []struct {
			Mail       string `json:"mail"`
			Password   string `json:"password"`
			SessionKey string `json:"sessionKey"`
		} `json:"accounts"`
	}
	if !bind(c, &req) {
		return
	}
	accounts := make([]dao.ClaudeAccount, 0, len(req.Accounts)+1)
	if strings.TrimSpace(req.Mail) != "" || req.Password != "" || strings.TrimSpace(req.SessionKey) != "" {
		accounts = append(accounts, dao.ClaudeAccount{Mail: req.Mail, Password: req.Password, SessionKey: req.SessionKey, Plan: "free", Status: 1})
	}
	for _, item := range req.Accounts {
		accounts = append(accounts, dao.ClaudeAccount{Mail: item.Mail, Password: item.Password, SessionKey: item.SessionKey, Plan: "free", Status: 1})
	}
	if len(accounts) == 0 {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("mail account or accounts are required"))
		return
	}
	if len(accounts) > s.cfg.MaxDispatchCount {
		fail(c, http.StatusBadRequest, "TOO_MANY_ACCOUNTS", fmt.Errorf("accounts cannot exceed %d", s.cfg.MaxDispatchCount))
		return
	}
	created, duplicates := 0, 0
	errorsByRow := make([]gin.H, 0)
	createdIDs := make([]int64, 0, len(accounts))
	for index, account := range accounts {
		id, err := s.store.CreateAccount(c.Request.Context(), account)
		switch {
		case err == nil:
			created++
			createdIDs = append(createdIDs, id)
		case dao.IsUniqueError(err):
			duplicates++
		default:
			errorsByRow = append(errorsByRow, gin.H{"index": index, "mail": strings.TrimSpace(account.Mail), "message": err.Error()})
		}
	}
	key := currentAPIKey(c)
	s.store.Audit(c.Request.Context(), "api_key", key.ID, "add_free_accounts", "claude_account", "", fmt.Sprintf(`{"created":%d,"duplicates":%d,"errors":%d}`, created, duplicates, len(errorsByRow)), clientIP(c))
	ok(c, gin.H{"created": created, "duplicates": duplicates, "errors": errorsByRow, "ids": createdIDs})
}

func (s *Server) dispatchAccounts(c *gin.Context) {
	var req struct {
		Count int    `json:"count"`
		Plan  string `json:"plan"`
	}
	if !bind(c, &req) {
		return
	}
	if req.Count == 0 {
		req.Count = 1
	}
	if req.Count < 1 || req.Count > s.cfg.MaxDispatchCount {
		fail(c, 400, "INVALID_COUNT", fmt.Errorf("count must be between 1 and %d", s.cfg.MaxDispatchCount))
		return
	}
	if req.Plan != "" && req.Plan != "free" {
		fail(c, 400, "INVALID_PLAN", errors.New("this endpoint only dispatches free accounts"))
		return
	}
	requestID := strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	if requestID == "" {
		requestID = "req_" + randomToken(16)
	}
	items, err := s.store.DispatchAccounts(c.Request.Context(), currentAPIKey(c).ID, requestID, req.Count, s.cfg.DispatchLease, clientIP(c))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	accounts := make([]gin.H, 0, len(items))
	for _, item := range items {
		accounts = append(accounts, gin.H{"mail": item.Mail, "password": item.Password, "sessionKey": item.SessionKey, "plan": item.Plan})
	}
	var leaseExpiresAt *time.Time
	if len(items) > 0 {
		leaseExpiresAt = items[0].LockedUntil
	}
	ok(c, gin.H{"requestId": requestID, "leaseExpiresAt": leaseExpiresAt, "count": len(accounts), "accounts": accounts})
}

func (s *Server) releaseDispatchedAccounts(c *gin.Context) {
	var req struct {
		RequestID string   `json:"requestId"`
		Mails     []string `json:"mails"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.RequestID) == "" || len(req.Mails) == 0 || len(req.Mails) > s.cfg.MaxDispatchCount {
		fail(c, 400, "BAD_REQUEST", fmt.Errorf("requestId and 1-%d mails are required", s.cfg.MaxDispatchCount))
		return
	}
	key := currentAPIKey(c)
	released := 0
	errorsByItem := make([]gin.H, 0)
	for _, mail := range req.Mails {
		account, err := s.store.ReleaseDispatchedAccount(c.Request.Context(), key.ID, req.RequestID, mail)
		if err != nil {
			errorsByItem = append(errorsByItem, gin.H{"mail": strings.TrimSpace(mail), "message": err.Error()})
			continue
		}
		released++
		s.store.Audit(c.Request.Context(), "api_key", key.ID, "release_failed_dispatch", "claude_account", strconv.FormatInt(account.ID, 10), fmt.Sprintf(`{"requestId":%q}`, req.RequestID), clientIP(c))
	}
	status := http.StatusOK
	if released == 0 && len(errorsByItem) > 0 {
		status = http.StatusConflict
	}
	c.JSON(status, gin.H{"data": gin.H{"released": released, "errors": errorsByItem}})
}

func (s *Server) upgradeAccount(c *gin.Context) {
	var req struct {
		Mail       string     `json:"mail"`
		Plan       string     `json:"plan"`
		UpgradedAt *time.Time `json:"upgradedAt"`
		CardPoolID int64      `json:"cardPoolId"`
	}
	if !bind(c, &req) {
		return
	}
	when := time.Now()
	if req.UpgradedAt != nil {
		when = *req.UpgradedAt
	}
	if req.CardPoolID <= 0 {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("positive cardPoolId is required"))
		return
	}
	a, err := s.store.UpgradeAccount(c.Request.Context(), req.Mail, req.Plan, when, req.CardPoolID)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"id": a.ID, "mail": a.Mail, "plan": a.Plan, "cardPoolId": a.CardPoolID, "upgradedAt": a.UpgradedAt, "deliveryStatus": a.DeliveryStatus})
}

func (s *Server) dispatchCards(c *gin.Context) {
	var req struct {
		Count  int    `json:"count"`
		Source string `json:"source"`
	}
	if !bind(c, &req) {
		return
	}
	if req.Count == 0 {
		req.Count = 1
	}
	if req.Count < 1 || req.Count > s.cfg.MaxDispatchCount {
		fail(c, 400, "INVALID_COUNT", fmt.Errorf("count must be between 1 and %d", s.cfg.MaxDispatchCount))
		return
	}
	requestID := strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	if requestID == "" {
		requestID = "card_" + randomToken(16)
	}
	key := currentAPIKey(c)
	items, err := s.store.DispatchCards(c.Request.Context(), key.ID, requestID, req.Source, req.Count, clientIP(c))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	cards := make([]gin.H, 0, len(items))
	for _, item := range items {
		cards = append(cards, gin.H{"cardPoolId": item.ID, "source": item.Source, "cardId": item.CardID, "cardNo": item.CardNo, "expireMmyy": item.ExpireMMYY, "ccv": item.CCV})
	}
	s.store.Audit(c.Request.Context(), "api_key", key.ID, "dispatch_cards", "card_pool", "", fmt.Sprintf(`{"requestId":%q,"count":%d}`, requestID, len(cards)), clientIP(c))
	ok(c, gin.H{"requestId": requestID, "count": len(cards), "cards": cards})
}

func (s *Server) reportCards(c *gin.Context) {
	var req struct {
		RequestID string `json:"requestId"`
		Cards     []struct {
			CardPoolID int64  `json:"cardPoolId"`
			Status     string `json:"status"`
			Reason     string `json:"reason"`
		} `json:"cards"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.RequestID) == "" || len(req.Cards) == 0 || len(req.Cards) > s.cfg.MaxDispatchCount {
		fail(c, 400, "BAD_REQUEST", fmt.Errorf("requestId and 1-%d cards are required", s.cfg.MaxDispatchCount))
		return
	}
	key := currentAPIKey(c)
	reported := 0
	errorsByItem := make([]gin.H, 0)
	for _, item := range req.Cards {
		if strings.ToLower(strings.TrimSpace(item.Status)) != "unavailable" {
			errorsByItem = append(errorsByItem, gin.H{"cardPoolId": item.CardPoolID, "message": "status must be unavailable"})
			continue
		}
		card, err := s.store.ReportCardUnavailable(c.Request.Context(), key.ID, req.RequestID, item.CardPoolID)
		if err != nil {
			errorsByItem = append(errorsByItem, gin.H{"cardPoolId": item.CardPoolID, "message": err.Error()})
			continue
		}
		reported++
		s.store.Audit(c.Request.Context(), "api_key", key.ID, "report_card_unavailable", "card_pool", strconv.FormatInt(card.ID, 10), fmt.Sprintf(`{"requestId":%q,"reason":%q}`, req.RequestID, strings.TrimSpace(item.Reason)), clientIP(c))
	}
	status := http.StatusOK
	if reported == 0 && len(errorsByItem) > 0 {
		status = http.StatusConflict
	}
	c.JSON(status, gin.H{"data": gin.H{"reported": reported, "errors": errorsByItem}})
}
