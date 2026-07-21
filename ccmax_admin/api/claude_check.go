package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"ccmax/dao"

	"github.com/gin-gonic/gin"
	xproxy "golang.org/x/net/proxy"
)

const claudeBaseURL = "https://claude.ai"

type accountAliveResult struct {
	ID        int64     `json:"id"`
	Mail      string    `json:"mail"`
	Alive     bool      `json:"alive"`
	CheckedAt time.Time `json:"checkedAt"`
	Message   string    `json:"message"`
}

func (s *Server) checkAccountsAlive(c *gin.Context) {
	var req struct {
		IDs []int64 `json:"ids"`
	}
	if !bind(c, &req) {
		return
	}
	ids := uniquePositiveIDs(req.IDs)
	if len(ids) == 0 || len(ids) > 100 {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", errors.New("1-100 positive account ids are required"))
		return
	}
	accounts, err := s.store.AccountsByIDs(c.Request.Context(), ids)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	if len(accounts) != len(ids) {
		fail(c, http.StatusNotFound, "NOT_FOUND", errors.New("one or more accounts do not exist"))
		return
	}
	client, err := claudeCheckHTTPClient(s.cfg.ClaudeCheckProxy)
	if err != nil {
		fail(c, http.StatusInternalServerError, "INVALID_PROXY", err)
		return
	}

	jobs := make(chan dao.ClaudeAccount)
	results := make(chan accountAliveResult, len(accounts))
	workerCount := min(10, len(accounts))
	var workers sync.WaitGroup
	for range workerCount {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for account := range jobs {
				alive, message := checkClaudeSessionWithRetry(c.Request.Context(), client, s.claudeBaseURL, account.SessionKey)
				results <- accountAliveResult{ID: account.ID, Mail: account.Mail, Alive: alive, CheckedAt: time.Now(), Message: message}
			}
		}()
	}
	go func() {
		defer close(jobs)
		for _, account := range accounts {
			select {
			case jobs <- account:
			case <-c.Request.Context().Done():
				return
			}
		}
	}()
	go func() {
		workers.Wait()
		close(results)
	}()

	items := make([]accountAliveResult, 0, len(accounts))
	aliveCount := 0
	for result := range results {
		status := "dead"
		if result.Alive {
			status = "alive"
			aliveCount++
		}
		if err = s.store.SetAccountAliveStatus(c.Request.Context(), result.ID, status, result.CheckedAt); err != nil {
			handleStoreError(c, err)
			return
		}
		items = append(items, result)
	}
	if err = c.Request.Context().Err(); err != nil {
		return
	}
	sort.Slice(items, func(i, j int) bool { return items[i].ID < items[j].ID })
	s.store.Audit(c.Request.Context(), "admin", currentAdmin(c).ID, "check_alive", "claude_account", "", fmt.Sprintf(`{"count":%d}`, len(items)), clientIP(c))
	ok(c, gin.H{"total": len(items), "alive": aliveCount, "dead": len(items) - aliveCount, "results": items})
}

func uniquePositiveIDs(values []int64) []int64 {
	seen := make(map[int64]struct{}, len(values))
	result := make([]int64, 0, len(values))
	for _, value := range values {
		if value <= 0 {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}

func checkClaudeSessionWithRetry(ctx context.Context, client *http.Client, baseURL, sessionKey string) (bool, string) {
	message := "请求失败"
	for attempt := 0; attempt < 2; attempt++ {
		alive, currentMessage := checkClaudeSession(ctx, client, baseURL, sessionKey)
		if alive {
			return true, "存活"
		}
		message = currentMessage
		if attempt == 0 {
			select {
			case <-time.After(time.Second):
			case <-ctx.Done():
				return false, "检测已取消"
			}
		}
	}
	return false, message
}

func checkClaudeSession(ctx context.Context, client *http.Client, baseURL, sessionKey string) (bool, string) {
	deviceID := randomUUID()
	anonymousID := "claudeai.v1." + randomUUID()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+"/api/organizations", nil)
	if err != nil {
		return false, "创建请求失败"
	}
	req.Header.Set("Accept", "*/*")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9")
	req.Header.Set("Anthropic-Anonymous-Id", anonymousID)
	req.Header.Set("Anthropic-Client-Platform", "web_claude_ai")
	req.Header.Set("Anthropic-Client-Version", "1.0.0")
	req.Header.Set("Anthropic-Device-Id", deviceID)
	req.Header.Set("Cache-Control", "no-cache")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Pragma", "no-cache")
	req.Header.Set("Priority", "u=1, i")
	req.Header.Set("Referer", "https://claude.ai/chats")
	req.Header.Set("Sec-Ch-Ua", `"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"`)
	req.Header.Set("Sec-Ch-Ua-Mobile", "?0")
	req.Header.Set("Sec-Ch-Ua-Platform", `"macOS"`)
	req.Header.Set("Sec-Fetch-Dest", "empty")
	req.Header.Set("Sec-Fetch-Mode", "cors")
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
	req.AddCookie(&http.Cookie{Name: "sessionKey", Value: strings.TrimSpace(sessionKey)})
	req.AddCookie(&http.Cookie{Name: "anthropic-device-id", Value: deviceID})
	resp, err := client.Do(req)
	if err != nil {
		return false, "请求失败"
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		return false, fmt.Sprintf("HTTP %d", resp.StatusCode)
	}
	var organizations []json.RawMessage
	if err = json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(&organizations); err != nil {
		return false, "响应格式异常"
	}
	return true, "存活"
}

func claudeCheckHTTPClient(rawProxy string) (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 20
	transport.MaxIdleConnsPerHost = 20
	rawProxy = strings.TrimSpace(rawProxy)
	if rawProxy == "" {
		return &http.Client{Transport: transport, Timeout: 15 * time.Second}, nil
	}
	parsed, err := url.Parse(rawProxy)
	if err != nil || parsed.Host == "" {
		return nil, errors.New("CLAUDE_CHECK_PROXY is invalid")
	}
	switch strings.ToLower(parsed.Scheme) {
	case "http", "https":
		transport.Proxy = http.ProxyURL(parsed)
	case "socks5", "socks5h":
		var auth *xproxy.Auth
		if parsed.User != nil {
			password, _ := parsed.User.Password()
			auth = &xproxy.Auth{User: parsed.User.Username(), Password: password}
		}
		dialer, dialErr := xproxy.SOCKS5("tcp", parsed.Host, auth, xproxy.Direct)
		if dialErr != nil {
			return nil, errors.New("CLAUDE_CHECK_PROXY cannot initialize")
		}
		transport.Proxy = nil
		transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
			return dialer.Dial(network, address)
		}
	default:
		return nil, errors.New("CLAUDE_CHECK_PROXY must use http, https, socks5 or socks5h")
	}
	return &http.Client{Transport: transport, Timeout: 15 * time.Second}, nil
}

func randomUUID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return randomToken(16)
	}
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	encoded := hex.EncodeToString(value[:])
	return encoded[0:8] + "-" + encoded[8:12] + "-" + encoded[12:16] + "-" + encoded[16:20] + "-" + encoded[20:32]
}
