package api

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"ccmax/dao"

	"github.com/gin-gonic/gin"
)

func (s *Server) listMailAccounts(c *gin.Context) {
	pageNumber, pageSize := page(c)
	enabled, _ := strconv.Atoi(c.Query("enabled"))
	items, total, err := s.store.ListMailAccounts(c.Request.Context(), pageNumber, pageSize, c.Query("q"), c.Query("platform"), c.Query("status"), enabled)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	stats, err := s.store.MailAccountStats(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total, "stats": stats})
}

func (s *Server) mailAccountStatus(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, http.StatusBadRequest, "BAD_ID", err)
		return
	}
	var req struct {
		Enabled int `json:"enabled"`
	}
	if !bind(c, &req) {
		return
	}
	if err = s.store.SetMailAccountEnabled(c.Request.Context(), id, req.Enabled); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "set_status", "mail_account", strconv.FormatInt(id, 10), fmt.Sprintf(`{"enabled":%d}`, req.Enabled), clientIP(c))
	ok(c, true)
}

func (s *Server) deleteMailAccount(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, http.StatusBadRequest, "BAD_ID", err)
		return
	}
	if err = s.store.DeleteMailAccount(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "delete", "mail_account", strconv.FormatInt(id, 10), "{}", clientIP(c))
	ok(c, true)
}

func (s *Server) importMailAccounts(c *gin.Context) {
	var req struct {
		Lines    string `json:"lines"`
		Platform string `json:"platform"`
	}
	if !bind(c, &req) {
		return
	}
	created, duplicates := 0, 0
	errorsByRow := []string{}
	for index, raw := range strings.Split(strings.ReplaceAll(req.Lines, "\r\n", "\n"), "\n") {
		if strings.TrimSpace(raw) == "" {
			continue
		}
		mail, password, platform, err := dao.ParseMailAccountLine(raw, req.Platform)
		if err == nil {
			_, err = s.store.CreateMailAccount(c.Request.Context(), mail, password, platform)
		}
		switch {
		case err == nil:
			created++
		case dao.IsUniqueError(err):
			duplicates++
		default:
			errorsByRow = append(errorsByRow, fmt.Sprintf("第 %d 行: %v", index+1, err))
		}
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "import", "mail_account", "", fmt.Sprintf(`{"platform":%q,"created":%d,"duplicates":%d,"errors":%d}`, strings.ToLower(strings.TrimSpace(req.Platform)), created, duplicates, len(errorsByRow)), clientIP(c))
	ok(c, gin.H{"created": created, "duplicates": duplicates, "errors": errorsByRow})
}

func (s *Server) dispatchMailAccount(c *gin.Context) {
	var req struct {
		RequestID string `json:"requestId"`
		Platform  string `json:"platform"`
	}
	if !bind(c, &req) {
		return
	}
	requestID := strings.TrimSpace(req.RequestID)
	if requestID == "" {
		requestID = strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	}
	if requestID == "" {
		requestID = "mail_" + randomToken(16)
	}
	key := currentAPIKey(c)
	item, err := s.store.DispatchMailAccount(c.Request.Context(), key.ID, requestID, req.Platform, s.cfg.DispatchLease, clientIP(c))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "api_key", key.ID, "dispatch", "mail_account", strconv.FormatInt(item.ID, 10), fmt.Sprintf(`{"requestId":%q,"platform":%q}`, requestID, item.Platform), clientIP(c))
	ok(c, gin.H{
		"requestId":      requestID,
		"leaseExpiresAt": item.LockedUntil,
		"account": gin.H{
			"mailAccountId": item.ID,
			"mail":          item.Mail,
			"password":      item.Password,
			"platform":      item.Platform,
		},
	})
}

func (s *Server) reportMailAccountUsed(c *gin.Context) {
	var req struct {
		RequestID         string `json:"requestId"`
		MailAccountID     int64  `json:"mailAccountId"`
		ClaudeAccountMail string `json:"claudeAccountMail"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.RequestID) == "" || req.MailAccountID <= 0 || strings.TrimSpace(req.ClaudeAccountMail) == "" {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("requestId, positive mailAccountId and claudeAccountMail are required"))
		return
	}
	key := currentAPIKey(c)
	item, err := s.store.UseMailAccount(c.Request.Context(), key.ID, req.RequestID, req.MailAccountID, req.ClaudeAccountMail)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "api_key", key.ID, "mark_used", "mail_account", strconv.FormatInt(item.ID, 10), fmt.Sprintf(`{"requestId":%q,"claudeAccountId":%d}`, req.RequestID, *item.ClaudeAccountID), clientIP(c))
	ok(c, gin.H{
		"mailAccountId":     item.ID,
		"status":            item.Status,
		"claudeAccountId":   item.ClaudeAccountID,
		"claudeAccountMail": item.ClaudeAccountMail,
		"usedAt":            item.UsedAt,
	})
}
