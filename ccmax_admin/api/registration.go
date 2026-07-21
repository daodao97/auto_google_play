package api

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"ccmax/dao"

	"github.com/gin-gonic/gin"
)

const registrationCredentialSource = "claude_register"

type registrationRunState struct {
	RunID          string         `json:"runId"`
	Status         string         `json:"status"`
	Platform       string         `json:"platform"`
	RequestedCount int            `json:"requestedCount"`
	ImportedCount  int            `json:"importedCount"`
	ConsumedCount  int            `json:"consumedCount"`
	StartedAt      time.Time      `json:"startedAt"`
	FinishedAt     *time.Time     `json:"finishedAt,omitempty"`
	Error          string         `json:"error,omitempty"`
	Summary        map[string]any `json:"summary,omitempty"`
	Tasks          []any          `json:"tasks,omitempty"`
	dbID           int64
	lockID         string
	accounts       map[string]dao.MailAccount
}

type registrationStartResponse struct {
	OK     bool   `json:"ok"`
	RunID  string `json:"run_id"`
	Count  int    `json:"count"`
	Error  string `json:"error"`
	Issues []any  `json:"issues"`
}

type registrationUpstreamStatus struct {
	Running bool           `json:"running"`
	RunID   string         `json:"run_id"`
	Summary map[string]any `json:"summary"`
	Tasks   []any          `json:"tasks"`
}

type registrationStartInput struct {
	Platform      string  `json:"platform"`
	Count         int     `json:"count"`
	Concurrency   int     `json:"concurrency"`
	RetryMax      int     `json:"retryMax"`
	ProxyMode     string  `json:"proxyMode"`
	ProxyTemplate *string `json:"proxyTemplate"`
	MailFastPath  bool    `json:"mailFastPath"`
}

type registrationLaunchError struct {
	status int
	code   string
	err    error
}

func (e *registrationLaunchError) Error() string { return e.err.Error() }

func (s *Server) registrationOverview(c *gin.Context) {
	s.loadRegistrationState(c.Request.Context())
	_, credentialErr := s.store.Credential(c.Request.Context(), registrationCredentialSource)
	platform := strings.ToLower(strings.TrimSpace(c.DefaultQuery("platform", "mailcom")))
	available, availableErr := s.store.AvailableMailAccountCount(c.Request.Context(), platform)
	if availableErr != nil {
		handleStoreError(c, availableErr)
		return
	}
	s.registrationMu.Lock()
	state := cloneRegistrationState(s.registrationRun)
	s.registrationMu.Unlock()
	schedule, scheduleErr := s.store.RegistrationSchedule(c.Request.Context())
	if scheduleErr != nil {
		handleStoreError(c, scheduleErr)
		return
	}
	ok(c, gin.H{
		"baseUrl":         s.registrationBaseURL,
		"tokenConfigured": credentialErr == nil,
		"available":       available,
		"run":             state,
		"schedule":        schedule,
	})
}

func (s *Server) updateRegistrationSchedule(c *gin.Context) {
	var input struct {
		Enabled       bool   `json:"enabled"`
		Platform      string `json:"platform"`
		Count         int    `json:"count"`
		Concurrency   int    `json:"concurrency"`
		RetryMax      int    `json:"retryMax"`
		ProxyMode     string `json:"proxyMode"`
		ProxyTemplate string `json:"proxyTemplate"`
		MailFastPath  bool   `json:"mailFastPath"`
	}
	if !bind(c, &input) {
		return
	}
	proxyTemplate := input.ProxyTemplate
	startInput := registrationStartInput{
		Platform: input.Platform, Count: input.Count, Concurrency: input.Concurrency, RetryMax: input.RetryMax,
		ProxyMode: input.ProxyMode, ProxyTemplate: &proxyTemplate, MailFastPath: input.MailFastPath,
	}
	if err := normalizeRegistrationInput(&startInput); err != nil {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", err)
		return
	}
	if startInput.ProxyMode != "override" {
		proxyTemplate = ""
	}
	admin := currentAdmin(c)
	schedule := dao.RegistrationSchedule{
		Enabled: input.Enabled, Platform: startInput.Platform, Count: startInput.Count,
		Concurrency: startInput.Concurrency, RetryMax: startInput.RetryMax, ProxyMode: startInput.ProxyMode,
		ProxyTemplate: proxyTemplate, MailFastPath: startInput.MailFastPath,
	}
	if err := s.store.SetRegistrationSchedule(c.Request.Context(), schedule, admin.ID); err != nil {
		handleStoreError(c, err)
		return
	}
	s.store.Audit(c.Request.Context(), "admin", admin.ID, "update_schedule", "registration", "1", fmt.Sprintf(`{"enabled":%t,"platform":%q,"count":%d}`, schedule.Enabled, schedule.Platform, schedule.Count), clientIP(c))
	if schedule.Enabled {
		s.startRegistrationScheduler()
	}
	saved, err := s.store.RegistrationSchedule(c.Request.Context())
	if err != nil {
		handleStoreError(c, err)
		return
	}
	ok(c, saved)
}

func (s *Server) startRegistrationScheduler() {
	s.registrationSchedulerOnce.Do(func() {
		go func() {
			ticker := time.NewTicker(s.registrationScheduleInterval)
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					s.runRegistrationSchedule()
				case <-s.registrationSchedulerStop:
					return
				}
			}
		}()
	})
}

func (s *Server) runRegistrationSchedule() {
	ctx := context.Background()
	schedule, err := s.store.RegistrationSchedule(ctx)
	if err != nil || !schedule.Enabled || schedule.UpdatedBy == nil {
		return
	}
	s.registrationMu.Lock()
	busy := s.registrationStarting || (s.registrationRun != nil && s.registrationRun.Status == "running")
	s.registrationMu.Unlock()
	if busy {
		return
	}
	available, err := s.store.AvailableMailAccountCount(ctx, schedule.Platform)
	if err != nil || available < 1 {
		return
	}
	count := schedule.Count
	if count > available {
		count = available
	}
	proxyTemplate := schedule.ProxyTemplate
	input := registrationStartInput{
		Platform: schedule.Platform, Count: count, Concurrency: schedule.Concurrency, RetryMax: schedule.RetryMax,
		ProxyMode: schedule.ProxyMode, ProxyTemplate: &proxyTemplate, MailFastPath: schedule.MailFastPath,
	}
	if err = normalizeRegistrationInput(&input); err != nil {
		return
	}
	_, _ = s.launchRegistration(ctx, input, *schedule.UpdatedBy, "scheduler", "start_scheduled")
}

func (s *Server) startRegistration(c *gin.Context) {
	var input registrationStartInput
	if !bind(c, &input) {
		return
	}
	if err := normalizeRegistrationInput(&input); err != nil {
		fail(c, http.StatusBadRequest, "BAD_REQUEST", err)
		return
	}
	admin := currentAdmin(c)
	state, err := s.launchRegistration(c.Request.Context(), input, admin.ID, clientIP(c), "start")
	if err != nil {
		var launchErr *registrationLaunchError
		if errors.As(err, &launchErr) {
			fail(c, launchErr.status, launchErr.code, launchErr.err)
		} else {
			fail(c, http.StatusInternalServerError, "REGISTRATION_START_ERROR", err)
		}
		return
	}
	ok(c, cloneRegistrationState(state))
}

func normalizeRegistrationInput(input *registrationStartInput) error {
	input.Platform = strings.ToLower(strings.TrimSpace(input.Platform))
	if input.Platform == "" {
		input.Platform = "mailcom"
	}
	if input.Platform != "mailcom" && input.Platform != "imap" {
		return errors.New("registration currently supports mailcom or imap accounts")
	}
	if input.Count < 1 || input.Count > 200 {
		return errors.New("count must be between 1 and 200")
	}
	if input.Concurrency == 0 {
		input.Concurrency = 2
	}
	if input.Concurrency < 1 || input.Concurrency > 10 || input.RetryMax < 0 || input.RetryMax > 5 {
		return errors.New("concurrency must be 1-10 and retryMax must be 0-5")
	}
	if input.ProxyMode == "" {
		input.ProxyMode = "configured"
	}
	if input.ProxyMode != "configured" && input.ProxyMode != "direct" && input.ProxyMode != "override" {
		return errors.New("invalid proxyMode")
	}
	if input.ProxyMode == "override" && (input.ProxyTemplate == nil || strings.TrimSpace(*input.ProxyTemplate) == "") {
		return errors.New("proxyTemplate is required for override mode")
	}
	return nil
}

func (s *Server) launchRegistration(ctx context.Context, input registrationStartInput, adminID int64, ip, auditAction string) (*registrationRunState, error) {
	s.registrationMu.Lock()
	if s.registrationStarting || (s.registrationRun != nil && s.registrationRun.Status == "running") {
		s.registrationMu.Unlock()
		return nil, &registrationLaunchError{http.StatusConflict, "REGISTRATION_RUNNING", errors.New("registration task is already running")}
	}
	s.registrationStarting = true
	s.registrationMu.Unlock()
	defer func() {
		s.registrationMu.Lock()
		s.registrationStarting = false
		s.registrationMu.Unlock()
	}()

	token, err := s.store.Credential(ctx, registrationCredentialSource)
	if err != nil {
		return nil, &registrationLaunchError{http.StatusBadRequest, "REGISTRATION_TOKEN_MISSING", errors.New("configure Claude-register token first")}
	}
	lockID := "registration_" + randomToken(18)
	accounts, err := s.store.ReserveMailAccountsForRegistration(ctx, input.Platform, input.Count, lockID, 6*time.Hour)
	if err != nil {
		return nil, &registrationLaunchError{http.StatusConflict, "INSUFFICIENT_MAIL_ACCOUNTS", err}
	}
	lines := make([]string, 0, len(accounts))
	accountMap := make(map[string]dao.MailAccount, len(accounts))
	for _, account := range accounts {
		lines = append(lines, account.Mail+"----"+account.Password)
		accountMap[strings.ToLower(account.Mail)] = account
	}
	payload := gin.H{
		"flow_mode":          "register",
		"proxy_mode":         input.ProxyMode,
		"proxy_template":     input.ProxyTemplate,
		"impersonate":        "",
		"concurrency":        input.Concurrency,
		"retry_max":          input.RetryMax,
		"auto_send":          true,
		"mail_fast_path":     input.MailFastPath,
		"resolve_exit_ip":    false,
		"mail_provider":      input.Platform,
		"mail_poll_interval": 3,
		"send_settle_delay":  nil,
		"accounts_text":      strings.Join(lines, "\n"),
	}
	var upstream registrationStartResponse
	if err = s.doRegistrationJSON(ctx, token, http.MethodPost, "/api/start", payload, &upstream); err != nil || !upstream.OK || upstream.RunID == "" {
		_ = s.store.ReleaseMailRegistration(ctx, lockID)
		if err == nil {
			err = errors.New(firstNonEmpty(upstream.Error, "Claude-register rejected the task"))
		}
		return nil, &registrationLaunchError{http.StatusBadGateway, "REGISTRATION_UPSTREAM_ERROR", err}
	}
	state := &registrationRunState{
		RunID:          upstream.RunID,
		Status:         "running",
		Platform:       input.Platform,
		RequestedCount: len(accounts),
		StartedAt:      time.Now(),
		lockID:         lockID,
		accounts:       accountMap,
	}
	dbID, persistErr := s.store.CreateRegistrationRun(ctx, dao.RegistrationRun{
		UpstreamRunID: upstream.RunID, Platform: input.Platform, RequestedCount: len(accounts), LockID: lockID, StartedAt: state.StartedAt,
	}, adminID, accounts)
	if persistErr != nil {
		var ignored map[string]any
		_ = s.doRegistrationJSON(context.Background(), token, http.MethodPost, "/api/stop", gin.H{}, &ignored)
		_ = s.store.ReleaseMailRegistration(ctx, lockID)
		return nil, &registrationLaunchError{http.StatusInternalServerError, "REGISTRATION_PERSIST_ERROR", persistErr}
	}
	state.dbID = dbID
	s.registrationMu.Lock()
	s.registrationRun = state
	s.registrationMu.Unlock()
	s.store.Audit(ctx, "admin", adminID, auditAction, "registration", upstream.RunID, fmt.Sprintf(`{"platform":%q,"count":%d}`, input.Platform, len(accounts)), ip)
	go s.monitorRegistration(upstream.RunID, token)
	return state, nil
}

func (s *Server) stopRegistration(c *gin.Context) {
	token, err := s.store.Credential(c.Request.Context(), registrationCredentialSource)
	if err != nil {
		handleStoreError(c, err)
		return
	}
	var result map[string]any
	if err = s.doRegistrationJSON(c.Request.Context(), token, http.MethodPost, "/api/stop", gin.H{}, &result); err != nil {
		fail(c, http.StatusBadGateway, "REGISTRATION_UPSTREAM_ERROR", err)
		return
	}
	ok(c, result)
}

func (s *Server) monitorRegistration(runID, token string) {
	ticker := time.NewTicker(s.registrationPollInterval)
	defer ticker.Stop()
	deadline := time.NewTimer(6 * time.Hour)
	defer deadline.Stop()
	for {
		select {
		case <-ticker.C:
			var upstream registrationUpstreamStatus
			err := s.doRegistrationJSON(context.Background(), token, http.MethodGet, "/api/status", nil, &upstream)
			if err != nil {
				s.setRegistrationError(runID, err.Error(), false)
				continue
			}
			if upstream.RunID != runID {
				s.finalizeRegistration(runID, token)
				return
			}
			s.registrationMu.Lock()
			if s.registrationRun != nil && s.registrationRun.RunID == runID {
				s.registrationRun.Summary = upstream.Summary
				s.registrationRun.Tasks = upstream.Tasks
				s.registrationRun.Error = ""
			}
			stateID := int64(0)
			if s.registrationRun != nil && s.registrationRun.RunID == runID {
				stateID = s.registrationRun.dbID
			}
			s.registrationMu.Unlock()
			if stateID > 0 {
				_ = s.store.UpdateRegistrationRunProgress(context.Background(), stateID, upstream.Summary, upstream.Tasks, "")
			}
			if !upstream.Running {
				s.finalizeRegistration(runID, token)
				return
			}
		case <-deadline.C:
			s.failRegistration(runID, "registration task timed out")
			return
		}
	}
}

func (s *Server) finalizeRegistration(runID, token string) {
	s.registrationMu.Lock()
	if s.registrationRun == nil || s.registrationRun.RunID != runID {
		s.registrationMu.Unlock()
		return
	}
	lockID := s.registrationRun.lockID
	dbID := s.registrationRun.dbID
	accounts := make(map[string]dao.MailAccount, len(s.registrationRun.accounts))
	for mail, account := range s.registrationRun.accounts {
		accounts[mail] = account
	}
	s.registrationMu.Unlock()
	imported, consumed := 0, 0
	errorsByAccount := []string{}
	passLines, err := s.fetchRegistrationFile(context.Background(), token, runID, "kyc_pass.txt")
	if err != nil {
		s.failRegistration(runID, err.Error())
		return
	}
	for _, line := range passLines {
		mail, sessionKey, valid := registrationResult(line)
		account, exists := accounts[mail]
		if !valid || !exists {
			continue
		}
		_, created, importErr := s.store.ImportRegisteredClaudeAccount(context.Background(), account.ID, lockID, sessionKey)
		if importErr != nil {
			errorsByAccount = append(errorsByAccount, mail+": "+importErr.Error())
			_ = s.store.ConsumeMailRegistration(context.Background(), account.ID, lockID)
			consumed++
			s.setRegistrationTaskClaudeStatus(runID, mail, "failed")
		} else {
			imported++
			if created {
				s.setRegistrationTaskClaudeStatus(runID, mail, "added")
			} else {
				s.setRegistrationTaskClaudeStatus(runID, mail, "linked")
			}
		}
		delete(accounts, mail)
	}
	for _, filename := range []string{"kyc_required.txt", "kyc_unknown.txt", "kyc_dead.txt"} {
		lines, fetchErr := s.fetchRegistrationFile(context.Background(), token, runID, filename)
		if fetchErr != nil {
			errorsByAccount = append(errorsByAccount, fetchErr.Error())
			continue
		}
		for _, line := range lines {
			mail, _, valid := registrationResult(line)
			account, exists := accounts[mail]
			if valid && exists {
				if consumeErr := s.store.ConsumeMailRegistration(context.Background(), account.ID, lockID); consumeErr == nil {
					consumed++
				}
				s.setRegistrationTaskClaudeStatus(runID, mail, "not_eligible")
				delete(accounts, mail)
			}
		}
	}
	_ = s.store.ReleaseMailRegistration(context.Background(), lockID)
	for mail := range accounts {
		s.setRegistrationTaskClaudeStatus(runID, mail, "not_added")
	}
	now := time.Now()
	s.registrationMu.Lock()
	if s.registrationRun != nil && s.registrationRun.RunID == runID {
		s.registrationRun.Status = "completed"
		s.registrationRun.ImportedCount = imported
		s.registrationRun.ConsumedCount = consumed
		s.registrationRun.FinishedAt = &now
		s.registrationRun.Error = strings.Join(errorsByAccount, "; ")
	}
	summary := s.registrationRun.Summary
	tasks := s.registrationRun.Tasks
	s.registrationMu.Unlock()
	if dbID > 0 {
		_ = s.store.UpdateRegistrationRunProgress(context.Background(), dbID, summary, tasks, strings.Join(errorsByAccount, "; "))
		_ = s.store.FinishRegistrationRun(context.Background(), dbID, "completed", imported, consumed, strings.Join(errorsByAccount, "; "))
	}
}

func (s *Server) setRegistrationTaskClaudeStatus(runID, mail, status string) {
	s.registrationMu.Lock()
	defer s.registrationMu.Unlock()
	if s.registrationRun == nil || s.registrationRun.RunID != runID {
		return
	}
	for _, raw := range s.registrationRun.Tasks {
		task, ok := raw.(map[string]any)
		if !ok || !strings.EqualFold(fmt.Sprint(task["email"]), mail) {
			continue
		}
		task["claude_account_status"] = status
		return
	}
}

func (s *Server) failRegistration(runID, message string) {
	s.registrationMu.Lock()
	if s.registrationRun == nil || s.registrationRun.RunID != runID {
		s.registrationMu.Unlock()
		return
	}
	lockID := s.registrationRun.lockID
	dbID := s.registrationRun.dbID
	s.registrationMu.Unlock()
	_ = s.store.ReleaseMailRegistration(context.Background(), lockID)
	s.setRegistrationError(runID, message, true)
	if dbID > 0 {
		_ = s.store.FinishRegistrationRun(context.Background(), dbID, "failed", 0, 0, message)
	}
}

func (s *Server) setRegistrationError(runID, message string, finished bool) {
	s.registrationMu.Lock()
	defer s.registrationMu.Unlock()
	if s.registrationRun == nil || s.registrationRun.RunID != runID {
		return
	}
	s.registrationRun.Error = message
	if finished {
		now := time.Now()
		s.registrationRun.Status = "failed"
		s.registrationRun.FinishedAt = &now
	}
}

func (s *Server) doRegistrationJSON(ctx context.Context, token, method, path string, input, output any) error {
	var body io.Reader
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(s.registrationBaseURL, "/")+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if input != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := s.registrationHTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Claude-register HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	if output != nil && len(data) > 0 {
		return json.Unmarshal(data, output)
	}
	return nil
}

func (s *Server) fetchRegistrationFile(ctx context.Context, token, runID, filename string) ([]string, error) {
	path := "/api/runs/" + url.PathEscape(runID) + "/" + url.PathEscape(filename)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(s.registrationBaseURL, "/")+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := s.registrationHTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("read %s: Claude-register HTTP %d", filename, resp.StatusCode)
	}
	lines := []string{}
	scanner := bufio.NewScanner(io.LimitReader(resp.Body, 5<<20))
	for scanner.Scan() {
		if line := strings.TrimSpace(scanner.Text()); line != "" {
			lines = append(lines, line)
		}
	}
	return lines, scanner.Err()
}

func registrationResult(line string) (string, string, bool) {
	parts := strings.Split(strings.TrimSpace(line), "----")
	if len(parts) < 3 {
		return "", "", false
	}
	mail := strings.ToLower(strings.TrimSpace(parts[0]))
	sessionKey := strings.TrimSpace(parts[len(parts)-1])
	return mail, sessionKey, mail != "" && sessionKey != ""
}

func cloneRegistrationState(state *registrationRunState) any {
	if state == nil {
		return nil
	}
	clone := *state
	clone.accounts = nil
	clone.lockID = ""
	clone.dbID = 0
	if state.Summary != nil {
		clone.Summary = make(map[string]any, len(state.Summary))
		for key, value := range state.Summary {
			clone.Summary[key] = value
		}
	}
	clone.Tasks = append([]any(nil), state.Tasks...)
	return clone
}

func (s *Server) loadRegistrationState(ctx context.Context) {
	s.registrationMu.Lock()
	if s.registrationRun != nil {
		s.registrationMu.Unlock()
		return
	}
	s.registrationMu.Unlock()
	run, accounts, err := s.store.LatestRegistrationRun(ctx)
	if err != nil {
		return
	}
	accountMap := make(map[string]dao.MailAccount, len(accounts))
	for _, account := range accounts {
		accountMap[strings.ToLower(account.Mail)] = account
	}
	state := &registrationRunState{
		dbID: run.ID, RunID: run.UpstreamRunID, Status: run.Status, Platform: run.Platform,
		RequestedCount: run.RequestedCount, ImportedCount: run.ImportedCount, ConsumedCount: run.ConsumedCount,
		StartedAt: run.StartedAt, FinishedAt: run.FinishedAt, Error: run.Error, lockID: run.LockID, accounts: accountMap,
	}
	_ = json.Unmarshal([]byte(run.SummaryJSON), &state.Summary)
	_ = json.Unmarshal([]byte(run.TasksJSON), &state.Tasks)
	s.registrationMu.Lock()
	if s.registrationRun == nil {
		s.registrationRun = state
	}
	s.registrationMu.Unlock()
}

func (s *Server) resumeRegistrationMonitor() {
	s.loadRegistrationState(context.Background())
	s.registrationMu.Lock()
	state := s.registrationRun
	s.registrationMu.Unlock()
	if state == nil || state.Status != "running" {
		return
	}
	token, err := s.store.Credential(context.Background(), registrationCredentialSource)
	if err != nil {
		return
	}
	go s.monitorRegistration(state.RunID, token)
}
