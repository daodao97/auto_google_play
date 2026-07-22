package api

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"ccmax/dao"

	"github.com/gin-gonic/gin"
)

type redeemUpstreamResponse struct {
	Success bool            `json:"success"`
	Data    json.RawMessage `json:"data"`
	Error   struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

type redeemTaskMeta struct {
	TaskID string `json:"taskId"`
	Status string `json:"status"`
}

func uuidCode() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	h := hex.EncodeToString(b[:])
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32], nil
}

func sessionUserEmail(session string) (string, error) {
	var payload struct {
		AccessToken string `json:"accessToken"`
	}
	if err := json.Unmarshal([]byte(session), &payload); err != nil || strings.TrimSpace(payload.AccessToken) == "" {
		return "", errors.New("session must contain accessToken")
	}
	parts := strings.Split(payload.AccessToken, ".")
	if len(parts) < 2 {
		return "", errors.New("accessToken must be a valid JWT")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return "", errors.New("accessToken JWT payload is invalid")
	}
	var claims struct {
		Email   string `json:"email"`
		Profile struct {
			Email string `json:"email"`
		} `json:"https://api.openai.com/profile"`
	}
	if err := json.Unmarshal(raw, &claims); err != nil {
		return "", errors.New("accessToken JWT claims are invalid")
	}
	email := strings.TrimSpace(claims.Profile.Email)
	if email == "" {
		email = strings.TrimSpace(claims.Email)
	}
	if email == "" {
		return "", errors.New("accessToken JWT does not contain user email")
	}
	return email, nil
}

func (s *Server) generateChatGPTCDKs(c *gin.Context) {
	var req struct {
		SKU      string `json:"sku"`
		Quantity int    `json:"quantity"`
		OrderNo  string `json:"orderNo"`
		Remark   string `json:"remark"`
		Format   string `json:"format"`
	}
	if !bind(c, &req) {
		return
	}
	if req.Quantity < 1 || req.Quantity > 1000 {
		fail(c, 400, "BAD_REQUEST", errors.New("quantity must be between 1 and 1000"))
		return
	}
	if !dao.ValidChatGPTSKU(req.SKU) {
		fail(c, 400, "INVALID_SKU", errors.New("sku must be one of: plus, pro, prolite"))
		return
	}
	if req.Format != "" && !strings.EqualFold(req.Format, "uuid") {
		fail(c, 400, "INVALID_FORMAT", errors.New("format must be uuid"))
		return
	}
	if strings.TrimSpace(req.OrderNo) == "" {
		now := time.Now()
		req.OrderNo = fmt.Sprintf("CDK-%s-%03d", now.Format("20060102-150405"), now.Nanosecond()/int(time.Millisecond))
	}
	codes := make([]string, req.Quantity)
	for i := range codes {
		code, err := uuidCode()
		if err != nil {
			handleStoreError(c, err)
			return
		}
		codes[i] = code
	}
	items, err := s.store.CreateChatGPTCDKs(c.Request.Context(), codes, req.SKU, req.OrderNo, req.Remark, currentAdmin(c).ID)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "generate", "chatgpt_cdk", "", fmt.Sprintf(`{"quantity":%d,"sku":%q,"orderNo":%q,"remark":%q}`, len(items), strings.ToLower(req.SKU), strings.TrimSpace(req.OrderNo), strings.TrimSpace(req.Remark)), clientIP(c))
	ok(c, gin.H{"items": items, "count": len(items)})
}

func (s *Server) listChatGPTCDKs(c *gin.Context) {
	p, z := page(c)
	items, total, err := s.store.ListChatGPTCDKs(c.Request.Context(), p, z, c.Query("q"), c.Query("sku"), c.Query("status"))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total})
}

func (s *Server) listChatGPTTasks(c *gin.Context) {
	p, z := page(c)
	items, total, err := s.store.ListChatGPTTasks(c.Request.Context(), p, z, c.Query("q"), c.Query("status"))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"items": items, "total": total})
}

func (s *Server) getChatGPTTask(c *gin.Context) {
	id, err := idParam(c)
	if err != nil {
		fail(c, 400, "BAD_ID", err)
		return
	}
	item, err := s.store.ChatGPTTaskByID(c.Request.Context(), id)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, gin.H{"task": item, "session": item.Session})
}

func (s *Server) exportChatGPTCDKs(c *gin.Context) {
	items, _, err := s.store.ListChatGPTCDKs(c.Request.Context(), 1, 1000000, c.Query("q"), c.Query("sku"), c.Query("status"))
	if err != nil {
		handleStoreError(c, err)
		return
	}
	c.Header("Content-Type", "text/csv; charset=utf-8")
	c.Header("Content-Disposition", `attachment; filename="chatgpt-cdks-`+time.Now().Format("20060102-150405")+`.csv"`)
	_, _ = c.Writer.Write([]byte{0xef, 0xbb, 0xbf})
	w := csv.NewWriter(c.Writer)
	_ = w.Write([]string{"id", "code", "sku", "used", "order_no", "remark", "used_at", "local_task_id", "created_at"})
	for _, item := range items {
		usedAt := ""
		if item.UsedAt != nil {
			usedAt = item.UsedAt.Format(time.RFC3339)
		}
		localTaskID := ""
		if item.TaskID != nil {
			localTaskID = strconv.FormatInt(*item.TaskID, 10)
		}
		_ = w.Write([]string{strconv.FormatInt(item.ID, 10), item.Code, item.SKU, strconv.FormatBool(item.Used), item.OrderNo, item.Remark, usedAt, localTaskID, item.CreatedAt.Format(time.RFC3339)})
	}
	w.Flush()
}

func (s *Server) checkChatGPTCDK(c *gin.Context) {
	var req struct {
		Code string `json:"code"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.Code) == "" {
		fail(c, 400, "BAD_REQUEST", errors.New("code is required"))
		return
	}
	item, err := s.store.ChatGPTCDKByCode(c.Request.Context(), req.Code)
	if err != nil {
		if dao.IsNoRows(err) {
			fail(c, 404, "CDK_NOT_FOUND", errors.New("cdk not found"))
		} else {
			handleStoreError(c, err)
		}
		return
	}
	ok(c, gin.H{"code": item.Code, "sku": item.SKU, "available": item.Status == "available", "used": item.Used, "status": item.Status})
}

func (s *Server) redeemChatGPTCDK(c *gin.Context) {
	var req struct {
		Code    string `json:"code"`
		Channel string `json:"channel"`
		Session string `json:"session"`
	}
	if !bind(c, &req) {
		return
	}
	if strings.TrimSpace(req.Code) == "" || strings.TrimSpace(req.Channel) == "" {
		fail(c, 400, "BAD_REQUEST", errors.New("code and channel are required"))
		return
	}
	if !json.Valid([]byte(req.Session)) {
		fail(c, 400, "INVALID_SESSION", errors.New("session must be a valid JSON string"))
		return
	}
	userEmail, err := sessionUserEmail(req.Session)
	if err != nil {
		fail(c, 400, "INVALID_SESSION", err)
		return
	}
	if s.chatGPTRedeemBaseURL == "" || s.chatGPTRedeemAPIKey == "" {
		fail(c, 503, "REDEEM_NOT_CONFIGURED", errors.New("ChatGPT redeem service is not configured"))
		return
	}
	item, err := s.store.ClaimChatGPTCDK(c.Request.Context(), req.Code)
	if err != nil {
		if dao.IsNoRows(err) {
			fail(c, 404, "CDK_NOT_FOUND", errors.New("cdk not found"))
		} else if strings.Contains(err.Error(), "not available") {
			fail(c, 409, "CDK_NOT_AVAILABLE", err)
		} else {
			handleStoreError(c, err)
		}
		return
	}
	apiKeyID := int64(0)
	if key := currentAPIKey(c); key != nil {
		apiKeyID = key.ID
	}
	localTask, err := s.store.CreateChatGPTTask(c.Request.Context(), *item, userEmail, req.Session, strings.TrimSpace(req.Channel), clientIP(c), apiKeyID)
	if err != nil {
		_ = s.store.ReleaseChatGPTCDK(c.Request.Context(), item.ID)
		handleStoreError(c, err)
		return
	}
	body, _ := json.Marshal(gin.H{"sku": item.SKU, "channel": strings.TrimSpace(req.Channel), "session": req.Session})
	upstream, status, err := s.callChatGPTRedeem(c, http.MethodPost, "/api/redeem/tasks", body)
	if err != nil || status < 200 || status >= 300 || !upstream.Success {
		code, message, resultJSON := "REDEEM_UPSTREAM_ERROR", "", ""
		if upstream != nil {
			code, message = upstream.Error.Code, upstream.Error.Message
			if encoded, encodeErr := json.Marshal(upstream); encodeErr == nil {
				resultJSON = string(encoded)
			}
		}
		if code == "" {
			code = "REDEEM_UPSTREAM_ERROR"
		}
		if err != nil {
			message = err.Error()
		}
		if message == "" {
			message = "failed to create redeem task"
		}
		_ = s.store.FailChatGPTTaskCreation(c.Request.Context(), localTask.ID, code, message, resultJSON)
		_ = s.store.ReleaseChatGPTCDK(c.Request.Context(), item.ID)
		if err != nil {
			failRedeemService(c, 502, "REDEEM_UPSTREAM_ERROR", err)
			return
		}
		failRedeemService(c, upstreamHTTPStatus(status), code, errors.New(message))
		return
	}
	var meta redeemTaskMeta
	if err = json.Unmarshal(upstream.Data, &meta); err != nil || meta.TaskID == "" {
		_ = s.store.FailChatGPTTaskCreation(c.Request.Context(), localTask.ID, "INVALID_UPSTREAM_RESPONSE", "redeem service response did not include taskId", string(upstream.Data))
		_ = s.store.ReleaseChatGPTCDK(c.Request.Context(), item.ID)
		failRedeemService(c, 502, "INVALID_UPSTREAM_RESPONSE", errors.New("redeem service response did not include taskId"))
		return
	}
	if err = s.store.FinalizeChatGPTTaskCreated(c.Request.Context(), item.ID, localTask.ID, meta.TaskID, meta.Status, string(upstream.Data)); err != nil {
		handleStoreError(c, err)
		return
	}
	actorType := "consumer"
	if apiKeyID > 0 {
		actorType = "api_key"
	}
	s.store.Audit(c.Request.Context(), actorType, apiKeyID, "redeem", "chatgpt_cdk", strconv.FormatInt(item.ID, 10), fmt.Sprintf(`{"localTaskId":%d,"remoteTaskId":%q}`, localTask.ID, meta.TaskID), clientIP(c))
	ok(c, gin.H{"taskId": localTask.HashID, "status": meta.Status, "createdAt": localTask.CreatedAt})
}

func (s *Server) getPublicChatGPTRedeemTask(c *gin.Context) {
	var req struct {
		Code   string `json:"code"`
		TaskID string `json:"taskId"`
	}
	if !bind(c, &req) {
		return
	}
	req.Code, req.TaskID = strings.TrimSpace(req.Code), strings.TrimSpace(req.TaskID)
	if req.Code == "" || req.TaskID == "" {
		fail(c, 400, "BAD_REQUEST", errors.New("code and taskId are required"))
		return
	}
	localTask, err := s.store.ChatGPTTaskByHashAndCode(c.Request.Context(), req.TaskID, req.Code)
	if err != nil {
		if dao.IsNoRows(err) {
			fail(c, 404, "TASK_NOT_FOUND", errors.New("task not found"))
		} else {
			handleStoreError(c, err)
		}
		return
	}
	if localTask.RemoteTaskID == "" {
		fail(c, 409, "TASK_NOT_READY", errors.New("任务尚未创建完成"))
		return
	}
	upstream, status, err := s.callChatGPTRedeem(c, http.MethodGet, "/api/redeem/tasks/"+url.PathEscape(localTask.RemoteTaskID), nil)
	if err != nil {
		failRedeemService(c, 502, "REDEEM_UPSTREAM_ERROR", err)
		return
	}
	if status < 200 || status >= 300 || !upstream.Success {
		message := upstream.Error.Message
		if message == "" {
			message = "failed to query redeem task"
		}
		code := upstream.Error.Code
		if code == "" {
			code = "REDEEM_UPSTREAM_ERROR"
		}
		failRedeemService(c, upstreamHTTPStatus(status), code, errors.New(message))
		return
	}
	var meta struct {
		Status string `json:"status"`
		Error  struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(upstream.Data, &meta); err != nil {
		failRedeemService(c, 502, "INVALID_UPSTREAM_RESPONSE", err)
		return
	}
	_ = s.store.UpdateChatGPTTask(c.Request.Context(), localTask.RemoteTaskID, meta.Status, meta.Error.Code, meta.Error.Message, string(upstream.Data))
	var responseData map[string]any
	if err := json.Unmarshal(upstream.Data, &responseData); err != nil {
		failRedeemService(c, 502, "INVALID_UPSTREAM_RESPONSE", err)
		return
	}
	responseData["taskId"] = localTask.HashID
	ok(c, responseData)
}

func (s *Server) getChatGPTRedeemTask(c *gin.Context) {
	taskID := strings.TrimSpace(c.Param("taskId"))
	if taskID == "" {
		fail(c, 400, "BAD_REQUEST", errors.New("taskId is required"))
		return
	}
	localTask, err := s.store.ChatGPTTaskByHashAndAPIKey(c.Request.Context(), taskID, currentAPIKey(c).ID)
	if err != nil {
		if dao.IsNoRows(err) {
			fail(c, 404, "TASK_NOT_FOUND", errors.New("task not found"))
		} else {
			handleStoreError(c, err)
		}
		return
	}
	if localTask.RemoteTaskID == "" {
		fail(c, 409, "TASK_NOT_READY", errors.New("task is not ready"))
		return
	}
	upstream, status, err := s.callChatGPTRedeem(c, http.MethodGet, "/api/redeem/tasks/"+url.PathEscape(localTask.RemoteTaskID), nil)
	if err != nil {
		fail(c, 502, "REDEEM_UPSTREAM_ERROR", err)
		return
	}
	if status < 200 || status >= 300 || !upstream.Success {
		message := upstream.Error.Message
		if message == "" {
			message = "failed to query redeem task"
		}
		code := upstream.Error.Code
		if code == "" {
			code = "REDEEM_UPSTREAM_ERROR"
		}
		fail(c, upstreamHTTPStatus(status), code, errors.New(message))
		return
	}
	var meta struct {
		Status string `json:"status"`
		Error  struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(upstream.Data, &meta); err != nil {
		fail(c, 502, "INVALID_UPSTREAM_RESPONSE", err)
		return
	}
	_ = s.store.UpdateChatGPTTask(c.Request.Context(), localTask.RemoteTaskID, meta.Status, meta.Error.Code, meta.Error.Message, string(upstream.Data))
	var responseData map[string]any
	if err := json.Unmarshal(upstream.Data, &responseData); err != nil {
		fail(c, 502, "INVALID_UPSTREAM_RESPONSE", err)
		return
	}
	responseData["taskId"] = localTask.HashID
	ok(c, responseData)
}

func (s *Server) callChatGPTRedeem(c *gin.Context, method, path string, body []byte) (*redeemUpstreamResponse, int, error) {
	req, err := http.NewRequestWithContext(c.Request.Context(), method, s.chatGPTRedeemBaseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Authorization", "Bearer "+s.chatGPTRedeemAPIKey)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := s.chatGPTRedeemHTTPClient.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("redeem service request failed: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, resp.StatusCode, err
	}
	var result redeemUpstreamResponse
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, resp.StatusCode, errors.New("redeem service returned invalid JSON")
	}
	return &result, resp.StatusCode, nil
}

func upstreamHTTPStatus(status int) int {
	if status >= 400 && status <= 599 {
		return status
	}
	return http.StatusBadGateway
}

func failRedeemService(c *gin.Context, status int, code string, err error) {
	if currentAPIKey(c) == nil {
		fail(c, http.StatusBadGateway, "REDEEM_SERVICE_UNAVAILABLE", errors.New("升级服务暂时不可用，请稍后重试"))
		return
	}
	fail(c, status, code, err)
}
