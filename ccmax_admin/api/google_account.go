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

func (s *Server) listGoogleAccounts(c *gin.Context) {
	pageNumber, pageSize := page(c)
	enabled, _ := strconv.Atoi(c.Query("enabled"))
	items, total, err := s.store.ListGoogleAccounts(c.Request.Context(), pageNumber, pageSize, c.Query("q"), c.Query("status"), enabled)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	stats, err := s.store.GoogleAccountStats(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total, "stats": stats})
}

func (s *Server) googleAccountStatus(c *gin.Context) {
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
	if err = s.store.SetGoogleAccountEnabled(c.Request.Context(), id, req.Enabled); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "set_status", "google_account", strconv.FormatInt(id, 10), fmt.Sprintf(`{"enabled":%d}`, req.Enabled), clientIP(c))
	ok(c, true)
}

func (s *Server) deleteGoogleAccount(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, http.StatusBadRequest, "BAD_ID", err)
		return
	}
	if err = s.store.DeleteGoogleAccount(c.Request.Context(), id); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "delete", "google_account", strconv.FormatInt(id, 10), "{}", clientIP(c))
	ok(c, true)
}

func (s *Server) importGoogleAccounts(c *gin.Context) {
	var req struct {
		Lines string `json:"lines"`
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
		mail, password, err := dao.ParseGoogleAccountLine(raw)
		if err == nil {
			_, err = s.store.CreateGoogleAccount(c.Request.Context(), mail, password)
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
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "import", "google_account", "", fmt.Sprintf(`{"created":%d,"duplicates":%d,"errors":%d}`, created, duplicates, len(errorsByRow)), clientIP(c))
	ok(c, gin.H{"created": created, "duplicates": duplicates, "errors": errorsByRow})
}

func (s *Server) dispatchGoogleAccount(c *gin.Context) {
	var req struct {
		RequestID string `json:"requestId"`
	}
	if !bind(c, &req) {
		return
	}
	requestID := strings.TrimSpace(req.RequestID)
	if requestID == "" {
		requestID = strings.TrimSpace(c.GetHeader("Idempotency-Key"))
	}
	if requestID == "" {
		requestID = "google_" + randomToken(16)
	}
	key := currentAPIKey(c)
	item, err := s.store.DispatchGoogleAccount(c.Request.Context(), key.ID, requestID, s.cfg.GoogleDispatchLease, clientIP(c))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "api_key", key.ID, "dispatch", "google_account", strconv.FormatInt(item.ID, 10), fmt.Sprintf(`{"requestId":%q}`, requestID), clientIP(c))
	ok(c, gin.H{
		"requestId":      requestID,
		"leaseExpiresAt": item.LockedUntil,
		"account": gin.H{
			"googleAccountId": item.ID,
			"mail":            item.Mail,
			"password":        item.Password,
		},
	})
}

func (s *Server) reportGoogleAccount(c *gin.Context) {
	var req struct {
		RequestID       string `json:"requestId"`
		GoogleAccountID int64  `json:"googleAccountId"`
		Status          string `json:"status"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.RequestID) == "" || req.GoogleAccountID <= 0 || strings.TrimSpace(req.Status) == "" {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("requestId, positive googleAccountId and status are required"))
		return
	}
	key := currentAPIKey(c)
	item, err := s.store.ReportGoogleAccount(c.Request.Context(), key.ID, req.RequestID, req.GoogleAccountID, req.Status)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "api_key", key.ID, "report_"+item.Status, "google_account", strconv.FormatInt(item.ID, 10), fmt.Sprintf(`{"requestId":%q,"status":%q}`, req.RequestID, item.Status), clientIP(c))
	ok(c, gin.H{
		"googleAccountId": item.ID,
		"status":          item.Status,
		"reportedAt":      item.ReportedAt,
	})
}
